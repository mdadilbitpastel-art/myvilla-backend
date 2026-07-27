"""
Whether a villa can be stayed in for a given date range and party size.

Guests book the whole villa, so availability is all-or-nothing: one active
booking that overlaps the requested nights takes the villa off the market for
those nights. Two things can make a villa unavailable, and a guest is told
which — being fully booked and being too small are different problems, and only
one of them is fixed by picking other dates.
"""

from datetime import date, time, timedelta
from typing import Dict, Iterable, List, Optional

from django.utils import timezone

from properties.models import DEFAULT_CHECK_IN_TIME, Booking, VillaBlockedDate


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


# --------------------------------------------------------------------------
# The booking window
#
# One rule, used by the calendar, the availability badge and the booking
# mutation alike: a villa is open for exactly `availability_days` consecutive
# dates, starting at the first date a guest could still arrive.
#
#   5 days, 27 Jul, check-in 2 PM, now 11 AM  →  27, 28, 29, 30, 31 Jul
#   the same villa at 3 PM (2 PM has gone by) →  28, 29, 30, 31 Jul, 1 Aug
#
# Nothing outside that span is bookable — not the day before, not the day
# after. The window slides with the clock, so it is always recomputed from
# "now" rather than stored.
# --------------------------------------------------------------------------


def check_in_cutoff(villa) -> time:
    """The hour a guest must arrive by — the villa's own, or the standard 2 PM."""
    return villa.check_in_time or DEFAULT_CHECK_IN_TIME


def window_days(villa) -> int:
    """How many dates the host is open for. At least one."""
    return max(1, villa.availability_days or 1)


def first_bookable_date(villa, now=None) -> date:
    """
    The earliest date a guest can still check in.

    Today, while the villa's check-in time is still ahead of us; tomorrow once
    it has passed — nobody can arrive today after the door closes, so today
    stops being a date at all rather than being offered and then refused.
    """
    now = timezone.localtime(now) if now is not None else timezone.localtime()
    if now.time() >= check_in_cutoff(villa):
        return now.date() + timedelta(days=1)
    return now.date()


def last_bookable_date(villa, now=None) -> date:
    """The last date a guest can check in — inclusive, and the last open night."""
    return first_bookable_date(villa, now) + timedelta(days=window_days(villa) - 1)


def window_end(villa, now=None) -> date:
    """
    Exclusive end of the window: the latest check-out a stay may have, i.e. the
    morning after the last open night. Half-open like every other date range
    here, so `check_out <= window_end` is the whole test.
    """
    return last_bookable_date(villa, now) + timedelta(days=1)


def unavailable_dates(villa, now=None) -> List[date]:
    """
    Every date inside the window a guest may NOT take: already booked by
    someone else, or closed by the host. The calendar disables exactly these
    (everything outside the window is disabled by the window itself).
    """
    start = first_bookable_date(villa, now)
    end = window_end(villa, now)  # exclusive

    taken = set()
    ranges = Booking.objects.filter(
        villa=villa,
        status=Booking.STATUS_ACTIVE,
        check_in__lt=end,
        check_out__gt=start,
    ).values_list("check_in", "check_out")
    for check_in, check_out in ranges:
        # Half-open: the check-out day is free for the next guest, so it is not
        # one of the occupied nights.
        night = max(check_in, start)
        stop = min(check_out, end)
        while night < stop:
            taken.add(night)
            night += timedelta(days=1)

    taken.update(
        VillaBlockedDate.objects.filter(
            villa=villa, date__gte=start, date__lt=end
        ).values_list("date", flat=True)
    )
    return sorted(taken)


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


def too_early_reason(villa, check_in: Optional[date], now=None) -> str:
    """
    Why this check-in date is already gone, or "" when it isn't. Separated out
    because "the date has passed" and "today's check-in time has passed" are
    different things to tell a guest, and only the second is a surprise.
    """
    if check_in is None:
        return ""
    first_open = first_bookable_date(villa, now)
    if check_in >= first_open:
        return ""
    today = timezone.localtime(now).date() if now is not None else timezone.localdate()
    if check_in < today:
        return "That date has passed"
    # check_in is today, and the door has closed on it.
    return f"Check-in closed at {check_in_cutoff(villa).strftime('%I:%M %p').lstrip('0')} today"


def unavailable_reasons(
    villa,
    guests: Optional[int] = None,
    free_from: Optional[date] = None,
    check_in: Optional[date] = None,
    check_out: Optional[date] = None,
    blocked_on: Optional[date] = None,
) -> list:
    """
    Every reason this villa can't take the stay — one entry per problem, built
    only from the filters the caller actually passed. An empty list means it
    can. The caller decides how to join them: a search that gave both a party
    size and dates shows the "too small" AND the "booked/closed" reasons
    together, so the guest sees the whole picture, not just the first problem.

    Pass `guests` only when a party size was asked about, and the date args only
    when dates were — so a filter the guest didn't set never invents a reason.
    """
    reasons = []
    if guests and villa.guests and guests > villa.guests:
        reasons.append(
            f"Sleeps up to {villa.guests} guest{'' if villa.guests == 1 else 's'}"
        )
    too_early = too_early_reason(villa, check_in)
    if too_early:
        reasons.append(too_early)
    if check_out:
        end = window_end(villa)
        if check_out > end:
            last = last_bookable_date(villa)
            reasons.append(f"Open for booking until {last.strftime('%d %b %Y')}")
    if blocked_on:
        reasons.append(f"Not available on {blocked_on.strftime('%d %b %Y')}")
    if free_from:
        # `free_from` is a check-out date, and a check-out day is not a night of
        # the stay — the villa is free that morning. So the last night actually
        # taken is the day before, and that is the date to name.
        last_night = free_from - timedelta(days=1)
        reasons.append(f"Booked until {last_night.strftime('%d %b %Y')}")
    return reasons


def unavailable_reason(
    villa,
    free_from: Optional[date],
    guests: Optional[int],
    check_in: Optional[date] = None,
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
    too_early = too_early_reason(villa, check_in)
    if too_early:
        return too_early
    if check_out:
        end = window_end(villa)
        if check_out > end:
            last = last_bookable_date(villa)
            return f"Open for booking until {last.strftime('%d %b %Y')}"
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
