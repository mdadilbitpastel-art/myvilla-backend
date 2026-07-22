"""Google Sign-In: verify the credential the browser gets from Google."""

import json
import urllib.parse
import urllib.request

from django.conf import settings
from graphql import GraphQLError

# `google-auth` is imported lazily, inside the ID-token branch below. At module
# level a missing package took down the whole GraphQL schema — every query, not
# just Google sign-in — the moment the image was a `pip install` behind.

# Only tokens minted for one of these issuers are accepted.
_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


_FAILED = "Google sign-in failed. Please try again."

# Google's servers, used for the access-token path.
_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _get_json(url: str, bearer: str = "") -> dict:
    request = urllib.request.Request(url)
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_google_credential(credential: str) -> dict:
    """
    Validate what the browser got back from Google and return the profile.

    Two shapes arrive here: an ID token (a JWT, from Google Identity Services'
    rendered button) or an OAuth access token (from the popup token client that
    backs our own "Continue with Google" button). Both are checked against our
    client id, so a token minted for some other site can't be replayed here.
    """
    credential = (credential or "").strip()
    if not credential:
        raise GraphQLError(_FAILED)

    client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
    if not client_id:
        # Misconfiguration, not user error — say so plainly instead of
        # pretending the account is at fault.
        raise GraphQLError("Google sign-in is not configured on this server.")

    if credential.count(".") != 2:
        return _verify_access_token(credential, client_id)

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError:
        raise GraphQLError(
            "Google sign-in is unavailable on this server (google-auth is not installed)."
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id
        )
    except Exception:
        raise GraphQLError(_FAILED)

    if claims.get("iss") not in _ISSUERS:
        raise GraphQLError("Google sign-in failed. Please try again.")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GraphQLError("Your Google account has no email address.")
    # An unverified address could belong to someone else — refusing it is what
    # stops a Google account from being used to seize an existing login.
    if not claims.get("email_verified"):
        raise GraphQLError("Your Google email address isn't verified.")

    return {
        "email": email,
        "full_name": (claims.get("name") or "").strip(),
        "avatar": (claims.get("picture") or "").strip(),
    }


def _verify_access_token(access_token: str, client_id: str) -> dict:
    """
    Access-token path. The audience check is the security-critical part: without
    it any site's Google token would unlock the matching MyVilla account.
    """
    query = urllib.parse.urlencode({"access_token": access_token})
    try:
        info = _get_json(f"{_TOKENINFO_URL}?{query}")
    except Exception:
        raise GraphQLError(_FAILED)

    if info.get("aud") != client_id and info.get("azp") != client_id:
        raise GraphQLError(_FAILED)

    try:
        profile = _get_json(_USERINFO_URL, bearer=access_token)
    except Exception:
        raise GraphQLError(_FAILED)

    email = (profile.get("email") or info.get("email") or "").strip().lower()
    if not email:
        raise GraphQLError("Your Google account has no email address.")
    verified = profile.get("email_verified")
    if verified in (False, "false"):
        raise GraphQLError("Your Google email address isn't verified.")

    return {
        "email": email,
        "full_name": (profile.get("name") or "").strip(),
        "avatar": (profile.get("picture") or "").strip(),
    }
