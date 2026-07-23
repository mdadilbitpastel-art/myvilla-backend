"""
WhatsApp notifications via the Meta WhatsApp Cloud API.

Sends the guest a greeting the moment a booking is paid for: the villa's photo
as the message header, then the trip's details in the body.

Two things about the Cloud API shape the code below:

  * A business-initiated message (which this is — the guest hasn't messaged us)
    MUST use a template Meta has approved in advance. Free text is only allowed
    inside the 24-hour window that opens when the *user* writes first. So the
    template path is the real one; the plain-text path exists for testing
    against a number that has just messaged the business.
  * Numbers must be E.164 without the leading "+" or any spaces.

Nothing here raises into the booking flow: a WhatsApp outage must not fail a
payment that already went through. Failures are logged and dropped, and the
frontend still offers its own "Share on WhatsApp" card as a fallback.

Configuration (all via environment, see settings.py):

    WHATSAPP_TOKEN              permanent access token of the system user
    WHATSAPP_PHONE_NUMBER_ID    id of the sending number (NOT the number itself)
    WHATSAPP_TEMPLATE_NAME      approved template's name; empty = plain text
    WHATSAPP_TEMPLATE_LANG      its language code, e.g. "en" or "en_US"
    WHATSAPP_TEMPLATE_URL_BUTTON  "true" if the template ends in a dynamic
                                  URL button (its suffix gets "villa/<id>")
    WHATSAPP_DEFAULT_DIAL_CODE  fallback country code for numbers saved without
    WHATSAPP_API_VERSION        Graph API version, default "v21.0"

The template to submit to Meta — category **Utility**, header **Image**, six
body variables in this order:

    Hi {{1}}! Your MyVilla booking is confirmed.

    Villa: {{2}}
    Where: {{3}}
    Stay: {{4}}
    Guests: {{5}}
    Total paid: {{6}}

    See you soon — MyVilla.com
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from django.conf import settings

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
_TIMEOUT = 10  # seconds — one slow call must not pin a worker thread for long


def _conf(name: str, default: str = "") -> str:
    return (getattr(settings, name, default) or "").strip()


def is_configured() -> bool:
    """True when a token and a sending number are both present."""
    return bool(_conf("WHATSAPP_TOKEN") and _conf("WHATSAPP_PHONE_NUMBER_ID"))


def to_e164(raw: str, fallback_dial: str = "") -> str:
    """
    Normalise a stored phone number to the digits-only form the API wants.

    Accepts what the app actually saves — "+91 98765 43210" from registration,
    or a bare local number typed into the booking form. A number with no country
    code at all is only usable if a default dial code is configured; guessing one
    would send the greeting to a stranger in another country.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    has_plus = raw.startswith("+") or raw.startswith("00")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("00"):
        digits = digits[2:]
    if not digits:
        return ""
    if has_plus:
        return digits
    dial = "".join(ch for ch in (fallback_dial or "") if ch.isdigit())
    if not dial:
        return ""
    # A local number sometimes carries a national trunk "0" — it never belongs
    # in front of a country code.
    return dial + digits.lstrip("0")


def _post(payload: dict) -> None:
    """POST one message to the Cloud API. Raises on transport/API error."""
    url = f"{GRAPH_HOST}/{_conf('WHATSAPP_API_VERSION', 'v21.0')}/{_conf('WHATSAPP_PHONE_NUMBER_ID')}/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_conf('WHATSAPP_TOKEN')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
        res.read()


def _template_payload(to: str, params: list[str], image_url: str, url_suffix: str) -> dict:
    components: list[dict] = []
    # A header image only works from a public https URL — a link Meta's servers
    # can fetch. Anything else (localhost, a data: URL) is left off, and the
    # message goes out as text-only rather than failing outright.
    if image_url.startswith("https://"):
        components.append(
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"link": image_url}}],
            }
        )
    components.append(
        {
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in params],
        }
    )
    if url_suffix and _conf("WHATSAPP_TEMPLATE_URL_BUTTON").lower() in ("1", "true", "yes"):
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": url_suffix}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": _conf("WHATSAPP_TEMPLATE_NAME"),
            "language": {"code": _conf("WHATSAPP_TEMPLATE_LANG", "en")},
            "components": components,
        },
    }


def _text_payload(to: str, body: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": True, "body": body},
    }


def _plain_body(params: list[str], image_url: str, link: str) -> str:
    name, villa, where, stay, guests, total = params
    lines = [
        f"Hi {name}! 🎉 Your MyVilla booking is confirmed.",
        "",
        f"🏡 {villa}",
        f"📍 {where}" if where else "",
        f"📅 {stay}",
        f"👥 {guests}",
        f"💳 Total paid: {total}",
        "",
        f"📸 {image_url}" if image_url.startswith("http") else "",
        f"🔗 {link}" if link else "",
        "",
        "See you soon — MyVilla.com",
    ]
    return "\n".join(line for line in lines if line != "")


def _fmt_date(d) -> str:
    return d.strftime("%d %b %Y")


def send_booking_confirmation(booking, cover_url: str = "") -> None:
    """
    Fire the greeting for one booking, in a background thread.

    Returns immediately: the guest's payment response must not wait on Meta,
    and nothing about the booking depends on the message arriving.
    """
    guest = booking.guest
    villa = booking.villa

    # The number the guest typed on the booking form wins — it's the one they
    # chose for this trip — but only when it carries a country code; otherwise
    # the account's number (saved with its dial code at registration) is better.
    candidates = [booking.contact_phone, getattr(guest, "phone_number", "")]
    to = ""
    for raw in candidates:
        to = to_e164(raw, _conf("WHATSAPP_DEFAULT_DIAL_CODE"))
        if to:
            break

    if not to:
        log.info("WhatsApp: booking %s has no usable phone number", booking.pk)
        return

    where = ", ".join(x for x in [villa.city, villa.country] if x)
    nights = booking.nights
    params = [
        (guest.full_name or guest.email.split("@")[0] or "there").strip(),
        villa.title,
        where,
        f"{_fmt_date(booking.check_in)} to {_fmt_date(booking.check_out)} "
        f"({nights} night{'' if nights == 1 else 's'})",
        f"{booking.guests} guest{'' if booking.guests == 1 else 's'}",
        f"${booking.total}",
    ]
    link = f"{_conf('FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/villa/{villa.pk}"

    if not is_configured():
        # Same spirit as the console e-mail backend in dev: show what would have
        # been sent instead of silently doing nothing.
        log.info(
            "WhatsApp not configured — would send to +%s:\n%s",
            to,
            _plain_body(params, cover_url, link),
        )
        return

    if _conf("WHATSAPP_TEMPLATE_NAME"):
        payload = _template_payload(to, params, cover_url, f"villa/{villa.pk}")
    else:
        payload = _text_payload(to, _plain_body(params, cover_url, link))

    def run() -> None:
        try:
            _post(payload)
            log.info("WhatsApp: booking confirmation sent for booking %s", booking.pk)
        except urllib.error.HTTPError as e:
            # Meta answers with a JSON error body that names the real problem
            # (template not approved, number not on WhatsApp, token expired) —
            # worth having in the log verbatim.
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # pragma: no cover - best effort only
                pass
            log.warning("WhatsApp send failed (%s) for booking %s: %s", e.code, booking.pk, detail)
        except Exception as e:  # network down, DNS, timeout …
            log.warning("WhatsApp send failed for booking %s: %s", booking.pk, e)

    threading.Thread(target=run, name=f"whatsapp-{booking.pk}", daemon=True).start()
