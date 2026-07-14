"""
Edge security middleware — sits in front of every request.

Responsibilities:
1. Reject requests from IPs that have been blocked for abuse (429/403).
2. Detect obvious malicious probes (exploit paths, traversal, injection
   markers) and block the offending IP.
3. Rate-limit per IP: a generous global limit plus a strict limit on
   auth-sensitive GraphQL operations (login/register/password reset) to stop
   brute-force and credential-stuffing.

State lives in Django's cache (LocMemCache in dev, Redis in prod via REDIS_URL),
so no schema/migration is involved.
"""

import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

# ---- tunables (all overridable from settings) -------------------------------
GENERAL_LIMIT = getattr(settings, "RL_GENERAL_LIMIT", 120)      # req / window
GENERAL_WINDOW = getattr(settings, "RL_GENERAL_WINDOW", 60)     # seconds
AUTH_LIMIT = getattr(settings, "RL_AUTH_LIMIT", 12)             # req / window
AUTH_WINDOW = getattr(settings, "RL_AUTH_WINDOW", 300)          # seconds
BLOCK_SECONDS = getattr(settings, "RL_BLOCK_SECONDS", 900)      # 15 min ban
VIOLATION_LIMIT = getattr(settings, "RL_VIOLATION_LIMIT", 5)    # strikes → ban
VIOLATION_WINDOW = getattr(settings, "RL_VIOLATION_WINDOW", 300)

# Substrings that never appear in legitimate traffic to this API.
MALICIOUS_MARKERS = (
    ".php", ".env", ".git", ".aws", ".ssh", "wp-admin", "wp-login",
    "phpmyadmin", "/vendor/", "/etc/passwd", "/bin/sh", "base64_",
    "<script", "union+select", "union select", "..%2f", "..%5c", "../",
    "..\\", "%00",
)

# GraphQL operations that must be tightly rate-limited.
AUTH_MARKERS = ("login", "register", "requestPasswordReset", "resetPassword")


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost")


def _hit(prefix: str, ident: str, window: int) -> int:
    """Fixed-window counter. Returns the request count in the current window."""
    bucket = int(time.time() // window)
    key = f"{prefix}:{ident}:{bucket}"
    try:
        cache.add(key, 0, timeout=window + 5)
        return cache.incr(key)
    except ValueError:
        # Key expired between add() and incr() — restart the window.
        cache.set(key, 1, timeout=window + 5)
        return 1


def _record_violation(ip: str) -> None:
    """Escalate: too many strikes in the window → block the IP outright."""
    if _is_loopback(ip):
        return  # never lock out local dev
    vkey = f"sec:viol:{ip}"
    try:
        cache.add(vkey, 0, timeout=VIOLATION_WINDOW)
        strikes = cache.incr(vkey)
    except ValueError:
        cache.set(vkey, 1, timeout=VIOLATION_WINDOW)
        strikes = 1
    if strikes >= VIOLATION_LIMIT:
        cache.set(f"sec:blk:{ip}", True, timeout=BLOCK_SECONDS)


def _blocked_response(retry_after: int) -> JsonResponse:
    resp = JsonResponse(
        {"errors": [{"message": "Too many requests. Please slow down and try again later."}]},
        status=429,
    )
    resp["Retry-After"] = str(retry_after)
    return resp


class SecurityGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ip = _client_ip(request)

        # 1) Already blocked?
        if cache.get(f"sec:blk:{ip}"):
            return _blocked_response(BLOCK_SECONDS)

        # 2) Malicious probe? (path or query string)
        haystack = (request.path + "?" + request.META.get("QUERY_STRING", "")).lower()
        if any(marker in haystack for marker in MALICIOUS_MARKERS):
            _record_violation(ip)
            return JsonResponse(
                {"errors": [{"message": "Request blocked."}]}, status=403
            )

        # 3) Global per-IP rate limit
        if _hit("sec:rl", ip, GENERAL_WINDOW) > GENERAL_LIMIT:
            _record_violation(ip)
            return _blocked_response(GENERAL_WINDOW)

        # 4) Strict limit on auth-sensitive GraphQL operations
        if request.method == "POST" and request.path.rstrip("/").endswith("graphql"):
            body = request.body  # cached by Django; GraphQLView can still read it
            text = body.decode("utf-8", "ignore") if body else ""
            if any(marker in text for marker in AUTH_MARKERS):
                if _hit("sec:rlauth", ip, AUTH_WINDOW) > AUTH_LIMIT:
                    _record_violation(ip)
                    return _blocked_response(AUTH_WINDOW)

        return self.get_response(request)
