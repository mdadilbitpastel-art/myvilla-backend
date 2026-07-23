"""
Whether a villa can be stayed in for a given date range and party size.

Guests book the whole villa, so availability is all-or-nothing: one active
booking that overlaps the requested nights takes the villa off the market for
those nights. Two things can make a villa unavailable, and a guest is told
which — being fully booked and being too small are different problems, and only
one of them is fixed by picking other dates.
"""

from datetime import date, timedelta
from typing import Dict, Iterable, Optional

from properties.models import Booking, VillaBlockedDate


def parse_date(value: Optional[str]) -> Optional[date]:
    """An ISO "YYYY-MM-DD" from the client, or None if absent/unparseable."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalise_range(check_in: Optional[date], check_out: Optional[date]):
    """
    Settle the window availability is judged over.

    A guest who searched by name alone gave no dates, but "is this villa free?"
    still has to mean something concrete — so it falls back to tonight. That
    keeps the badge on a name-only search honest without inventing a stay the
    guest never asked for.
    """
    if check_in and check_out and check_out > check_in:
        return check_in, check_out
    if check_in and not check_out:
        return check_in, check_in + timedelta(days=1)
    today = date.today()
    return today, today + timedelta(days=1)


def window_end(villa) -> date:
    """
    The last date this villa is open for booking: today plus the host's
    `availability_days`. Everything past it is not-yet-open rather than free —
    a host who only plans a few days out shouldn't be committed further.
    """
    return date.today() + timedelta(days=max(1, villa.availability_days or 1))


def booked_until(
    villa_ids: Iterable[int], check_in: date, check_out: date
) -> Dict[int, date]:
    """
    For each of the given villas that is taken over the range, the date it
    frees up again — the latest check-out among the bookings that clash.

    Half-open on purpose: a stay ending the morning another begins does NOT
    clash, which is why the comparisons are strict. `check_in < existing_out`
    and `check_out > existing_in` is the standard interval-overlap test, and it
    is the one rule every date-availability answer in the app goes through.
    """
    ids = [int(i) for i in villa_ids]
    if not ids:
        return {}
    clashing = Booking.objects.filter(
        villa_id__in=ids,
        status=Booking.STATUS_ACTIVE,
        check_in__lt=check_out,
        check_out__gt=check_in,
    ).values_list("villa_id", "check_out")

    free_from: Dict[int, date] = {}
    for villa_id, ends in clashing:
        villa_id = int(villa_id)
        if ends > free_from.get(villa_id, ends - timedelta(days=1)):
            free_from[villa_id] = ends
    return free_from


def blocked_nights(
    villa_ids: Iterable[int], check_in: date, check_out: date
) -> Dict[int, date]:
    """
    For each villa, the first night the host has closed by hand inside the
    range — or nothing, if none are. Half-open like everywhere else: the
    check-out day isn't a night of the stay, so closing it doesn't clash.
    """
    ids = [int(i) for i in villa_ids]
    if not ids:
        return {}
    rows = VillaBlockedDate.objects.filter(
        villa_id__in=ids, date__gte=check_in, date__lt=check_out
    ).values_list("villa_id", "date")

    first: Dict[int, date] = {}
    for villa_id, day in rows:
        villa_id = int(villa_id)
        if villa_id not in first or day < first[villa_id]:
            first[villa_id] = day
    return first


def is_blocked(villa_id: int, check_in: date, check_out: date) -> Optional[date]:
    """Single-villa form of `blocked_nights` — used when taking a booking."""
    return blocked_nights([villa_id], check_in, check_out).get(int(villa_id))


def is_booked(villa_id: int, check_in: date, check_out: date) -> bool:
    """Single-villa form of `booked_until` — used when taking a booking."""
    return bool(booked_until([villa_id], check_in, check_out))


def unavailable_reason(
    villa,
    free_from: Optional[date],
    guests: Optional[int],
    check_out: Optional[date] = None,
    blocked_on: Optional[date] = None,
) -> str:
    """
    Why this villa can't take the stay, or "" when it can.

    Ordered by what the guest can do about it. Capacity first: no other set of
    dates fixes a villa that simply doesn't sleep that many people. Then the
    host's window, then a night they've closed, then an actual clash.
    """
    if guests and villa.guests and guests > villa.guests:
        return f"Sleeps up to {villa.guests} guest{'' if villa.guests == 1 else 's'}"
    if check_out:
        end = window_end(villa)
        if check_out > end:
            return f"Open for booking until {end.strftime('%d %b %Y')}"
    if blocked_on:
        return f"Not available on {blocked_on.strftime('%d %b %Y')}"
    if free_from:
        # `free_from` is a check-out date, and a check-out day is not a night of
        # the stay — the villa is free that morning. So the last night actually
        # taken is the day before, and that is the date to name: a stay of the
        # 24th (checking out on the 25th) is "Booked until 24", not 25.
        last_night = free_from - timedelta(days=1)
        return f"Booked until {last_night.strftime('%d %b %Y')}"
    return ""
