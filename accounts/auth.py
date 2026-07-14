"""JWT helpers shared by the GraphQL layer."""

from typing import Optional

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


def tokens_for_user(user) -> dict:
    """Issue a fresh access + refresh token pair for a user."""
    refresh = RefreshToken.for_user(user)
    return {
        "access_token": str(refresh.access_token),
        "refresh_token": str(refresh),
    }


def get_authenticated_user(info) -> Optional["User"]:
    """
    Resolve the current user from the request's `Authorization: Bearer <token>`
    header. Returns None when there is no valid token.
    """
    request = info.context.request
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None

    raw_token = header.split(" ", 1)[1].strip()
    try:
        from rest_framework_simplejwt.tokens import AccessToken

        access = AccessToken(raw_token)
        return User.objects.get(pk=access["user_id"], is_active=True)
    except Exception:
        return None
