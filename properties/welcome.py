"""
The first-booking welcome offer.

A flat percentage off a guest's very first stay, applied automatically at
checkout — there is no code to type and nothing to claim. This is the one place
that decides who gets it and how much it takes off, so the payment page's
preview, the booking that freezes it, and the popup that advertises it can
never quote different numbers.
"""

from decimal import Decimal, ROUND_HALF_UP

from properties.models import Booking

# How much comes off a guest's first stay. A platform-funded welcome, not a
# host's coupon — see `better_of` for how the two interact.
FIRST_BOOKING_RATE = Decimal("0.25")

# What the popup and the checkout row call it.
HEADLINE = "25% off your first stay"
BLURB = (
    "New here? Your first booking gets 25% off the stay automatically — "
    "no code needed."
)


def percent_off() -> float:
    """The rate as a whole-number percentage, for display."""
    return float(FIRST_BOOKING_RATE * 100)


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_eligible(user) -> bool:
    """
    True when this guest has never booked before.

    Counts EVERY booking, cancelled ones included: the offer is for a first
    booking, and someone who booked and called it off has had theirs. That also
    closes the obvious loop — book, cancel, book again, discounted forever.
    """
    if user is None or not getattr(user, "pk", None):
        return False
    return not Booking.objects.filter(guest_id=user.pk).exists()


def discount_for(user, subtotal) -> Decimal:
    """What the welcome offer takes off this subtotal — 0 when not eligible."""
    subtotal = _money(subtotal)
    if subtotal <= 0 or not is_eligible(user):
        return Decimal("0.00")
    return min(_money(subtotal * FIRST_BOOKING_RATE), subtotal)


def better_of(welcome: Decimal, coupon: Decimal) -> str:
    """
    Which discount to apply when a guest has both. They do NOT stack: a first
    stay with a 25% welcome and a host's 20% code takes 25% off, not 45%. The
    guest always gets the larger of the two, and the host is never asked to
    fund both at once.

    Returns "welcome", "coupon" or "none".
    """
    if welcome <= 0 and coupon <= 0:
        return "none"
    return "welcome" if welcome >= coupon else "coupon"
