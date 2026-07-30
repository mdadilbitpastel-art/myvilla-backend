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


def get_user_from_request(request) -> Optional["User"]:
    """
    Resolve the current user from a request's `Authorization: Bearer <token>`
    header. Returns None when there is no valid token.

    Note this is the ONLY thing that knows who is calling: the token is decoded
    here and never written back onto `request.user`, so Django's own
    `request.user` stays anonymous even for a signed-in caller. Anything that
    needs the viewer must come through here (or `get_authenticated_user`) —
    reading `request.user` will silently see nobody.

    The result is cached on the request, so a resolver that serialises fifty
    rows decodes the token once rather than fifty times.
    """
    if request is None:
        return None
    if hasattr(request, "_myvilla_viewer"):
        return request._myvilla_viewer

    user = None
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        raw_token = header.split(" ", 1)[1].strip()
        try:
            from rest_framework_simplejwt.tokens import AccessToken

            access = AccessToken(raw_token)
            user = User.objects.get(pk=access["user_id"], is_active=True)
        except Exception:
            user = None

    request._myvilla_viewer = user
    return user


def get_authenticated_user(info) -> Optional["User"]:
    """The current user behind a GraphQL request, or None."""
    return get_user_from_request(info.context.request)
