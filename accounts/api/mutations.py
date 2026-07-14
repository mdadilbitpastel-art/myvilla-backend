import strawberry
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from graphql import GraphQLError

from accounts.auth import tokens_for_user
from accounts.security import require_authenticated_user
from .types import AuthPayload, TokenPayload, UserType

User = get_user_model()


@strawberry.type
class AuthMutation:
    @strawberry.mutation
    def register(
        self,
        email: str,
        password: str,
        phone_number: str = "",
        country: str = "",
        full_name: str = "",
    ) -> AuthPayload:
        email = email.strip().lower()
        phone_number = phone_number.strip()
        country = country.strip()
        full_name = full_name.strip()

        # --- Required fields (mirrors the frontend register form) ---
        if not email:
            raise GraphQLError("Email is required.")
        try:
            validate_email(email)
        except DjangoValidationError:
            raise GraphQLError("Enter a valid email address.")
        if not phone_number:
            raise GraphQLError("Phone number is required.")
        if not country:
            raise GraphQLError("Country or region is required.")
        if not password:
            raise GraphQLError("Password is required.")

        if User.objects.filter(email=email).exists():
            raise GraphQLError("An account with this email already exists.")

        # Enforce Django's password strength rules.
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise GraphQLError(" ".join(exc.messages))

        user = User.objects.create_user(
            email=email,
            password=password,
            phone_number=phone_number,
            country=country,
            full_name=full_name,
        )

        tokens = tokens_for_user(user)
        return AuthPayload(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            user=UserType.from_model(user),
        )

    @strawberry.mutation
    def login(self, email: str, password: str) -> AuthPayload:
        email = email.strip().lower()
        if not email or not password:
            raise GraphQLError("Email and password are required.")
        user = User.objects.filter(email=email).first()

        if user is None or not user.check_password(password):
            raise GraphQLError("Invalid email or password.")
        if not user.is_active:
            raise GraphQLError("This account is disabled.")

        tokens = tokens_for_user(user)
        return AuthPayload(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            user=UserType.from_model(user),
        )

    @strawberry.mutation
    def request_password_reset(self, email: str) -> bool:
        """
        Step 1 of the reset flow. Emails a reset link to the address if it
        belongs to an account. Always returns True so callers can't use this
        endpoint to discover which emails are registered.
        """
        from django.conf import settings
        from django.contrib.auth.tokens import default_token_generator
        from django.core.mail import send_mail
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        email = email.strip().lower()
        if not email:
            raise GraphQLError("Email is required.")

        user = User.objects.filter(email=email, is_active=True).first()
        if user is not None:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            link = (
                f"{settings.FRONTEND_URL.rstrip('/')}"
                f"/reset-password?uid={uid}&token={token}"
            )
            send_mail(
                subject="Reset your MyVilla password",
                message=(
                    "We received a request to reset your MyVilla password.\n\n"
                    f"Reset it here: {link}\n\n"
                    "This link is active for 15 minutes. If you didn't request "
                    "it, you can safely ignore this email."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True,
            )
        return True

    @strawberry.mutation
    def reset_password(self, uid: str, token: str, new_password: str) -> bool:
        """
        Step 2 of the reset flow. Verifies the emailed uid+token and sets a new
        password. The token is single-use — changing the password invalidates it.
        """
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_str
        from django.utils.http import urlsafe_base64_decode

        try:
            pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=pk, is_active=True)
        except Exception:
            raise GraphQLError("This reset link is invalid or has expired.")

        if not default_token_generator.check_token(user, token):
            raise GraphQLError("This reset link is invalid or has expired.")

        if not new_password:
            raise GraphQLError("Password is required.")
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            raise GraphQLError(" ".join(exc.messages))

        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        return True

    @strawberry.mutation
    def update_profile(
        self,
        info: strawberry.Info,
        full_name: str = "",
        gender: str = "",
        email: str = "",
        date_of_birth: str = "",
        address: str = "",
        emergency_contact: str = "",
    ) -> UserType:
        """Persist the Profile-Settings form. Requires a valid session."""
        user = require_authenticated_user(info)

        email = email.strip().lower()
        if not email:
            raise GraphQLError("Email is required.")
        try:
            validate_email(email)
        except DjangoValidationError:
            raise GraphQLError("Enter a valid email address.")
        if User.objects.filter(email=email).exclude(pk=user.pk).exists():
            raise GraphQLError("An account with this email already exists.")

        user.full_name = full_name.strip()
        user.gender = gender.strip()
        user.email = email
        user.date_of_birth = date_of_birth.strip()
        user.address = address.strip()
        user.emergency_contact = emergency_contact.strip()
        user.save(
            update_fields=[
                "full_name", "gender", "email", "date_of_birth",
                "address", "emergency_contact", "updated_at",
            ]
        )
        return UserType.from_model(user)

    @strawberry.mutation
    def update_avatar(self, info: strawberry.Info, image: str) -> UserType:
        """
        Set the current user's profile picture. `image` is a base64 data-URL
        (`data:image/...;base64,...`) already resized small by the client.
        Requires a valid session.
        """
        user = require_authenticated_user(info)

        image = (image or "").strip()
        if not image.startswith("data:image/"):
            raise GraphQLError("Please provide a valid image.")
        # Guard against oversized payloads (~700 KB of base64 ≈ 0.5 MB image).
        if len(image) > 700_000:
            raise GraphQLError("Image is too large. Please choose a smaller one.")

        user.avatar = image
        user.save(update_fields=["avatar", "updated_at"])
        return UserType.from_model(user)

    @strawberry.mutation
    def refresh_token(self, refresh_token: str) -> TokenPayload:
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import RefreshToken

        try:
            refresh = RefreshToken(refresh_token)
            access = str(refresh.access_token)
            # ROTATE_REFRESH_TOKENS is on, so hand back the (possibly rotated) refresh.
            return TokenPayload(access_token=access, refresh_token=str(refresh))
        except TokenError:
            raise GraphQLError("Invalid or expired refresh token.")
