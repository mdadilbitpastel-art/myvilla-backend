from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List

import strawberry
from django.db import transaction
from graphql import GraphQLError

from accounts.security import require_authenticated_user
from properties.images import data_url_to_file
from properties.models import Booking, Favorite, Villa, VillaImage
from .types import BookingInput, BookingType, VillaInput, VillaType

# Upper bound on images per villa (defensive; the UI allows fewer).
MAX_IMAGES = 15

# Platform service fee applied on top of the accommodation subtotal.
SERVICE_FEE_RATE = Decimal("0.141")

# Maximum nights allowed per booking (standard short-stay cap).
MAX_BOOKING_NIGHTS = 5


def _money(value) -> Decimal:
    """Round any numeric to 2 decimal places, half-up (currency)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _mask_account(raw: str) -> str:
    """Store payout accounts safely: keep only the last 4 digits, masked."""
    account = (raw or "").strip()
    if not account:
        return ""
    digits = _digits(account)
    if len(digits) >= 4:
        return "•••• " + digits[-4:]
    return account


def _validate_common(user, data: VillaInput):
    """
    Enforce the mandatory fields shared by create & update: personal details
    (Section 1, from the user's profile), villa details (2), pricing (5) and
    the payment method + account type (6). Card-number rules differ between
    create and update, so they're handled by each caller. Returns (title,
    accepted_payments).
    """
    # --- Section 1: Personal Details ---
    missing = [
        label
        for value, label in (
            (user.full_name, "full name"),
            (user.gender, "gender"),
            (user.email, "email"),
            (getattr(user, "date_of_birth", ""), "date of birth"),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise GraphQLError(
            "Complete your personal details first: " + ", ".join(missing) + "."
        )

    # --- Section 2: Villa Details ---
    title = (data.title or "").strip()
    if not title:
        raise GraphQLError("Villa name is required.")
    if not (data.description or "").strip():
        raise GraphQLError("A villa description is required.")
    if not (data.build_up_area or "").strip():
        raise GraphQLError("Villa dimensions are required.")
    if not (data.address or "").strip():
        raise GraphQLError("Villa address is required.")
    if data.bedrooms < 1:
        raise GraphQLError("Number of rooms must be at least 1.")
    if data.bathrooms < 1:
        raise GraphQLError("Number of bathrooms must be at least 1.")

    # --- Section 5: Pricing ---
    if data.price_per_night is None or data.price_per_night <= 0:
        raise GraphQLError("Please enter a valid price per night.")

    # --- Section 6: Payment method + account type ---
    accepted = [p.strip() for p in (data.accepted_payments or []) if p.strip()]
    if not accepted:
        raise GraphQLError("Select at least one payment method.")
    if not (data.payout_method or "").strip():
        raise GraphQLError("Please choose Credit or Debit Card.")

    return title, accepted


def _apply_fields(villa: Villa, data: VillaInput, title, accepted, payout_account):
    """Copy validated input onto a (new or existing) villa instance."""
    villa.title = title
    villa.property_type = (data.property_type or "").strip()
    villa.city = (data.city or "").strip()
    villa.country = (data.country or "").strip()
    villa.address = (data.address or "").strip()
    villa.description = (data.description or "").strip()
    villa.build_up_area = (data.build_up_area or "").strip()
    villa.bedrooms = max(0, data.bedrooms)
    villa.bathrooms = max(0, data.bathrooms)
    villa.guests = max(0, data.guests)
    villa.services = [s.strip() for s in (data.services or []) if s.strip()]
    villa.price_per_night = data.price_per_night
    villa.accepted_payments = accepted
    villa.payout_method = (data.payout_method or "").strip()
    villa.payout_account = payout_account


@strawberry.type
class PropertyMutation:
    @strawberry.mutation
    def create_villa(self, info: strawberry.Info, data: VillaInput) -> VillaType:
        """
        Create a villa owned by the current user, saving each provided image
        through the configured storage backend. Requires a valid session.
        Every section's mandatory fields are enforced here.
        """
        user = require_authenticated_user(info)
        title, accepted = _validate_common(user, data)

        # --- Section 3: Images (at least one, freshly uploaded) ---
        images = data.images or []
        if not images:
            raise GraphQLError("Please add at least one image.")
        if len(images) > MAX_IMAGES:
            raise GraphQLError(f"You can add up to {MAX_IMAGES} images.")

        # --- Card number: a full number is required on create ---
        if len(_digits(data.payout_account)) < 12:
            raise GraphQLError("Enter a valid card number.")

        files = [data_url_to_file(img) for img in images]
        payout_account = _mask_account(data.payout_account)

        with transaction.atomic():
            villa = Villa(owner=user)
            _apply_fields(villa, data, title, accepted, payout_account)
            villa.save()
            for f in files:
                VillaImage.objects.create(villa=villa, image=f)

        return VillaType.from_model(villa, request=info.context.request)

    @strawberry.mutation
    def update_villa(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        data: VillaInput,
        keep_image_ids: List[strawberry.ID] = strawberry.field(default_factory=list),
    ) -> VillaType:
        """
        Update a villa the current user owns. `keep_image_ids` are the existing
        photos to keep; any not listed are removed, and `data.images` (base64)
        are added as new photos. The final photo set must have at least one.
        Leave the card number blank to keep the existing (masked) one.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=id, owner=user).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        title, accepted = _validate_common(user, data)

        # --- Card number: keep existing unless a new full number is supplied ---
        incoming = _digits(data.payout_account)
        if len(incoming) >= 12:
            payout_account = _mask_account(data.payout_account)
        elif len(incoming) == 0 and villa.payout_account:
            payout_account = villa.payout_account  # unchanged
        else:
            raise GraphQLError("Enter a valid card number.")

        # --- Images: kept existing + newly uploaded, at least one total ---
        keep_ids = {str(i) for i in (keep_image_ids or [])}
        kept = [im for im in villa.images.all() if str(im.id) in keep_ids]
        new_images = data.images or []
        if len(kept) + len(new_images) < 1:
            raise GraphQLError("Please add at least one image.")
        if len(kept) + len(new_images) > MAX_IMAGES:
            raise GraphQLError(f"You can add up to {MAX_IMAGES} images.")

        new_files = [data_url_to_file(img) for img in new_images]

        with transaction.atomic():
            _apply_fields(villa, data, title, accepted, payout_account)
            villa.save()
            # Remove photos the user dropped (delete file + row).
            for im in villa.images.all():
                if str(im.id) not in keep_ids:
                    im.image.delete(save=False)
                    im.delete()
            for f in new_files:
                VillaImage.objects.create(villa=villa, image=f)

        villa.refresh_from_db()
        return VillaType.from_model(villa, request=info.context.request)

    @strawberry.mutation
    def delete_villa(self, info: strawberry.Info, id: strawberry.ID) -> bool:
        """
        Delete a villa the current user owns, along with its photos (files are
        removed from storage). Returns True on success. Requires a valid session.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=id, owner=user).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        with transaction.atomic():
            for im in villa.images.all():
                im.image.delete(save=False)  # drop the stored file (disk/Cloudinary)
                im.delete()
            villa.delete()
        return True

    @strawberry.mutation
    def toggle_favorite(self, info: strawberry.Info, villa_id: strawberry.ID) -> bool:
        """
        Add or remove a villa from the current user's wishlist. Returns the new
        state: True if now saved, False if removed. Requires a valid session.
        """
        user = require_authenticated_user(info)
        villa = Villa.objects.filter(pk=villa_id).first()
        if villa is None:
            raise GraphQLError("Villa not found.")
        fav = Favorite.objects.filter(user=user, villa=villa).first()
        if fav is not None:
            fav.delete()
            return False
        Favorite.objects.create(user=user, villa=villa)
        return True

    @strawberry.mutation
    def create_booking(self, info: strawberry.Info, data: BookingInput) -> BookingType:
        """
        Book a villa for the current user (the "Confirm and Pay" action).
        A guest may NOT book their own villa — that is rejected here as the
        single server-side enforcement gate. Totals are computed on the server.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=data.villa_id).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        # --- Core rule: you cannot book your own villa ---
        if villa.owner_id == user.id:
            raise GraphQLError("You cannot book your own villa.")

        # --- Dates ---
        try:
            check_in = date.fromisoformat((data.check_in or "").strip())
            check_out = date.fromisoformat((data.check_out or "").strip())
        except ValueError:
            raise GraphQLError("Please choose valid check-in and check-out dates.")
        # Check-in can't be in the past (standard booking rule).
        if check_in < date.today():
            raise GraphQLError("Check-in date cannot be in the past.")
        nights = (check_out - check_in).days
        if nights < 1:
            raise GraphQLError("Check-out must be after check-in.")
        if nights > MAX_BOOKING_NIGHTS:
            raise GraphQLError(
                f"You can book at most {MAX_BOOKING_NIGHTS} nights per stay."
            )

        # --- Guests ---
        guests = max(1, data.guests)

        # --- Payment details ---
        if not (data.payment_method or "").strip():
            raise GraphQLError("Please choose a card type.")
        if len(_digits(data.card_number)) < 12:
            raise GraphQLError("Enter a valid card number.")
        if not (data.expiration or "").strip():
            raise GraphQLError("Enter the card expiration date.")
        cvv = _digits(data.cvv)
        if len(cvv) < 3 or len(cvv) > 4:
            raise GraphQLError("Enter a valid CVV.")

        # --- Billing address (mandatory core fields) ---
        if not (data.billing_street or "").strip():
            raise GraphQLError("Enter your billing street name.")
        if not (data.billing_city or "").strip():
            raise GraphQLError("Enter your billing city.")
        if not (data.billing_country or "").strip():
            raise GraphQLError("Select your billing country or region.")

        # --- Additional information ---
        email = (data.contact_email or "").strip()
        if "@" not in email or "." not in email:
            raise GraphQLError("Enter a valid e-mail address.")

        # --- Money (frozen server-side) ---
        price = Decimal(str(villa.price_per_night))
        subtotal = _money(price * nights)
        service_fee = _money(subtotal * SERVICE_FEE_RATE)
        total = _money(subtotal + service_fee)

        booking = Booking(
            villa=villa,
            guest=user,
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            guests=guests,
            price_per_night=price,
            subtotal=subtotal,
            service_fee=service_fee,
            total=total,
            payment_method=(data.payment_method or "").strip(),
            card_last4=_mask_account(data.card_number),
            billing_street=(data.billing_street or "").strip(),
            billing_apartment=(data.billing_apartment or "").strip(),
            billing_city=(data.billing_city or "").strip(),
            billing_state=(data.billing_state or "").strip(),
            billing_zip=(data.billing_zip or "").strip(),
            billing_country=(data.billing_country or "").strip(),
            contact_email=email,
            contact_phone=(data.contact_phone or "").strip(),
        )
        booking.save()

        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def cancel_booking(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """Cancel one of the current user's own bookings."""
        user = require_authenticated_user(info)
        booking = Booking.objects.filter(pk=id, guest=user).first()
        if booking is None:
            raise GraphQLError("Booking not found.")
        booking.status = Booking.STATUS_CANCELLED
        booking.save(update_fields=["status", "updated_at"])
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def respond_booking(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """Host responds to a rent request on a villa they own."""
        user = require_authenticated_user(info)
        booking = (
            Booking.objects.select_related("villa", "guest")
            .filter(pk=id, villa__owner=user)
            .first()
        )
        if booking is None:
            raise GraphQLError("Rent request not found.")
        booking.host_responded = True
        booking.save(update_fields=["host_responded", "updated_at"])
        return BookingType.from_model(booking, request=info.context.request)
