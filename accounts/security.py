"""
Security helpers shared across the app.

- `get_client_ip`        — best-effort client IP (honours X-Forwarded-For).
- `require_authenticated_user` / `login_required` — enforce a valid JWT on
  GraphQL resolvers. On failure they raise a GraphQLError tagged
  `code = "UNAUTHENTICATED"` so the frontend can log the user out.
"""

from functools import wraps

from graphql import GraphQLError

from accounts.auth import get_authenticated_user


def get_client_ip(request) -> str:
    """Resolve the originating client IP, trusting the first X-Forwarded-For hop."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


class Unauthenticated(GraphQLError):
    """Raised when a protected resolver is reached without a valid session."""

    def __init__(self, message: str = "Authentication required."):
        super().__init__(message, extensions={"code": "UNAUTHENTICATED"})


def require_authenticated_user(info):
    """
    Return the current user or raise `Unauthenticated`. Every action that
    needs a logged-in user must funnel through this so unauthenticated
    requests are rejected *before* any work is done.
    """
    user = get_authenticated_user(info)
    if user is None or not getattr(user, "is_active", False):
        raise Unauthenticated()
    return user


def login_required(resolver):
    """
    Decorator for Strawberry resolvers. Validates the JWT first, injects the
    resolved user as `user=` (if the resolver accepts it), else just guards.
    """

    @wraps(resolver)
    def wrapper(self, info, *args, **kwargs):
        user = require_authenticated_user(info)
        if "user" in resolver.__code__.co_varnames:
            kwargs.setdefault("user", user)
        return resolver(self, info, *args, **kwargs)

    return wrapper
