import strawberry

from accounts.security import require_authenticated_user
from .types import UserType


@strawberry.type
class AccountQuery:
    @strawberry.field
    def me(self, info: strawberry.Info) -> UserType:
        """The currently authenticated user (requires a valid access token)."""
        # Validate the session first — unauthenticated callers get an
        # UNAUTHENTICATED error which the frontend turns into a logout.
        user = require_authenticated_user(info)
        return UserType.from_model(user)
