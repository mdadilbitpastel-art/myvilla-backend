"""
Coupon resolution — the single place that decides whether a code applies to a
villa and how much it takes off. Shared by the `validateCoupon` query (the
payment page's live preview) and by `createBooking` (the real, frozen apply),
so the two can never quote different numbers.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import Coupon


def _pretty(day) -> str:
    """A date as guests read it on a voucher: "30 Sep 2026"."""
    return day.strftime("%d %b %Y")


def normalise_code(raw: str) -> str:
    """How a code is stored and looked up: trimmed and upper-cased."""
    return (raw or "").strip().upper()


def money(value) -> Decimal:
    """Round any numeric to 2 decimal places, half-up (currency)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class CouponError(Exception):
    """A coupon that was supplied but can't be applied — the reason is the message."""


def resolve_coupon(code: str, villa) -> Coupon | None:
    """
    Find the coupon a guest's code names and confirm it applies to `villa`.

    Returns None for a blank code (no coupon asked for — not an error). Raises
    CouponError with a guest-facing reason when a code was given but is unknown,
    inactive, outside its validity period, or belongs to a different villa/host.
    """
    code = normalise_code(code)
    if not code:
        return None

    coupon = Coupon.objects.filter(code=code).select_related("villa").first()
    if coupon is None:
        raise CouponError("That coupon code isn't valid.")

    # The validity period, before the host's own switch: a code that has run
    # out should say so by date, which is what a guest holding it can check.
    # Both ends are inclusive — "valid until the 30th" works all day on the
    # 30th. Judged on the server's date, the same clock createBooking uses.
    today = timezone.localdate()
    if coupon.valid_until and today > coupon.valid_until:
        raise CouponError(f"This coupon expired on {_pretty(coupon.valid_until)}.")
    if not coupon.active:
        raise CouponError("This coupon is no longer active.")
    if coupon.valid_from and today < coupon.valid_from:
        raise CouponError(f"This coupon starts on {_pretty(coupon.valid_from)}.")

    # A villa-scoped coupon only ever applies to its own villa; a common coupon
    # applies to any villa its owner lists. Either way it must be the host's own
    # villa — one host's code can't discount another's listing.
    if coupon.villa_id is not None:
        if str(coupon.villa_id) != str(villa.id):
            raise CouponError("This coupon can't be used for this villa.")
    elif coupon.owner_id != villa.owner_id:
        raise CouponError("This coupon can't be used for this villa.")

    return coupon


def discount_for(coupon: Coupon, subtotal) -> Decimal:
    """
    The amount a coupon takes off a given accommodation subtotal, never more
    than the subtotal itself (a stay can't cost less than nothing).
    """
    subtotal = money(subtotal)
    if subtotal <= 0 or coupon is None:
        return Decimal("0.00")
    value = Decimal(str(coupon.discount_value))
    if coupon.discount_type == Coupon.TYPE_PERCENT:
        amount = money(subtotal * value / Decimal("100"))
    else:
        amount = money(value)
    if amount < 0:
        amount = Decimal("0.00")
    return min(amount, subtotal)


def validity_label(coupon: Coupon) -> str:
    """
    The validity period in one line, as a voucher would word it — "" when the
    coupon has no dates at all and simply runs until the host turns it off.
    """
    start, end = coupon.valid_from, coupon.valid_until
    if start and end:
        return f"Valid {_pretty(start)} – {_pretty(end)}"
    if end:
        return f"Valid until {_pretty(end)}"
    if start:
        return f"Valid from {_pretty(start)}"
    return ""


def days_left(coupon: Coupon, today=None) -> int:
    """
    Whole days until the coupon's last day, inclusive (so its final day is 1).
    -1 when it never expires — "no deadline" is not "zero days".
    """
    if not coupon.valid_until:
        return -1
    today = today or timezone.localdate()
    return max(0, (coupon.valid_until - today).days + 1)


def label_for(coupon: Coupon) -> str:
    """A short human label for a coupon's value, e.g. "20% off" or "$50 off"."""
    value = Decimal(str(coupon.discount_value))
    if coupon.discount_type == Coupon.TYPE_PERCENT:
        # Drop a trailing ".00" so it reads "20% off", not "20.00% off".
        num = value.quantize(Decimal("1")) if value == value.to_integral() else value
        return f"{num}% off"
    num = value.quantize(Decimal("1")) if value == value.to_integral() else money(value)
    return f"${num} off"
