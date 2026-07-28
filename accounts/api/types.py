import strawberry


@strawberry.type
class UserType:
    id: strawberry.ID
    email: str
    full_name: str
    phone_number: str
    country: str
    gender: str
    date_of_birth: str
    address: str
    emergency_contact: str
    avatar: str
    # Bank payout details, shared across all the host's villas. payout_account
    # is the MASKED account number ("•••• 1234"); "" when not set yet.
    payout_account_name: str
    payout_bank_name: str
    payout_ifsc: str
    payout_account: str

    @classmethod
    def from_model(cls, user) -> "UserType":
        return cls(
            id=strawberry.ID(str(user.id)),
            email=user.email,
            full_name=user.full_name,
            phone_number=user.phone_number,
            country=user.country,
            gender=getattr(user, "gender", "") or "",
            date_of_birth=getattr(user, "date_of_birth", "") or "",
            address=getattr(user, "address", "") or "",
            emergency_contact=getattr(user, "emergency_contact", "") or "",
            avatar=getattr(user, "avatar", "") or "",
            payout_account_name=getattr(user, "payout_account_name", "") or "",
            payout_bank_name=getattr(user, "payout_bank_name", "") or "",
            payout_ifsc=getattr(user, "payout_ifsc", "") or "",
            payout_account=getattr(user, "payout_account", "") or "",
        )


@strawberry.type
class AuthPayload:
    """Returned by register / login — the user plus a JWT token pair."""

    access_token: str
    refresh_token: str
    user: UserType


@strawberry.type
class TokenPayload:
    """Returned by refreshToken."""

    access_token: str
    refresh_token: str
