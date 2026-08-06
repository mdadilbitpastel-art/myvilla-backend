"""
Adding to a booking that has already been paid for.

A stay is not settled the moment it is booked. The guest may decide they do
want the airport pickup after all, or that they'd like two more nights on the
end — and neither should mean cancelling and booking again, which costs them a
cancellation fee and risks losing the villa to somebody else in between.

So a booking can GROW, and this is where growing is priced and carried out.
Two things can be added:

  * extra services the villa offers and this booking doesn't have yet, charged
    per night over the nights of the stay that haven't started;
  * nights, next to (or in the gap of) the nights already held, charged at the
    stay's own frozen nightly rate with the same fee and tax the checkout
    applied.

Nothing here ever REMOVES anything: this module only charges. Nights are given
up through `Booking.nights_cancellation_quote` — with its refund ladder — and a
service is dropped through `properties/removals.py`, which refunds the nights of
it that haven't started. Everything is priced on the server's clock at the
moment it is asked, exactly like a cancellation, so a quote the guest reads is
the arithmetic the button performs.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from django.utils import timezone

from properties import availability
from properties.models import Booking, BookingAddition

# Platform service fee and flat tax on the accommodation subtotal. The single
# source for both — `properties/api/mutations.py` imports them from here so a
# night added later is charged exactly what a night booked at checkout was.
SERVICE_FEE_RATE = Decimal("0.141")
TAX_RATE = Decimal("0.05")

MSG_CANCELLED = "This booking has been cancelled, so nothing can be added to it."
MSG_STAY_OVER = "This stay is over, so nothing can be added to it."
MSG_NO_NIGHTS_LEFT = (
    "Every night of this stay has already begun, so there is nothing left to "
    "add a service to."
)
MSG_AWAITING_ARRIVAL = (
    "Check-in time has passed, so this stay can't be changed until you've "
    "checked in. Check in first and you can add to it from there."
)
MSG_VILLA_REMOVED = (
    "This property is no longer listed on MyVilla, so nothing more can be "
    "bought on this stay. The stay itself goes ahead exactly as booked."
)


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pretty(day: date) -> str:
    return day.strftime("%d %b %Y")


def blocked_reason(booking: Booking, now=None) -> str:
    """
    Why this booking can't be CHANGED at all, or "" when it can.

    Three doors are shut. A cancelled booking is not a stay any more, and a stay
    every part of which is behind us has nothing left to sell.

    The third is the hour the guest is due. Up to it they are still planning a
    trip and may change it freely. On it the door shuts, arrived or not, because
    a stay whose check-in hour has passed with nobody in it is not yet anything
    to add to — the host is standing at the door, the grace period is running,
    and what this booking turns out to be is still open. Checking in settles it,
    and the door opens again on the part in front of the guest: a split stay has
    later parts to edit, an unbroken one has an end to extend.
    """
    now = now or timezone.now()
    if booking.status == Booking.STATUS_CANCELLED:
        return MSG_CANCELLED
    part = booking.current_part(now)
    if part is None:
        return MSG_STAY_OVER
    if part["checked_in_at"] is None and now >= part["opens_at"]:
        return MSG_AWAITING_ARRIVAL
    return ""


def purchase_blocked_reason(booking: Booking, now=None) -> str:
    """
    Why nothing more can be BOUGHT on this booking, or "" when something can.

    Every door `blocked_reason` shuts, plus one of its own: the host has taken
    the property off MyVilla. That distinction is the whole point of keeping the
    two apart. A removed listing must not sell anything further — no extra
    nights, no extra services, on a property nobody can book — but the guest is
    not thereby locked into what they already hold. Giving a service BACK for a
    refund goes through `blocked_reason`, and stays open: a refund takes nothing
    from a host who has walked away from the listing, and refusing it would trap
    a guest's money on a property that is gone.

    The same reasoning is why cancelling never consults either of these.
    """
    if booking.villa.is_deleted:
        return MSG_VILLA_REMOVED
    return blocked_reason(booking, now)


# --------------------------------------------------------------------------
# Extra services
# --------------------------------------------------------------------------


@dataclass
class ServiceQuote:
    """What adding a set of services to a booking would cost, right now."""

    # Each chosen service as it would be frozen onto the booking:
    # {"name", "price", "nights", "amount"}.
    services: List[dict] = field(default_factory=list)
    # The nights every one of them is charged over — the stay's remaining
    # nights, which is what the guest can actually be given.
    nights: List[date] = field(default_factory=list)
    amount: Decimal = Decimal("0.00")
    allowed: bool = False
    error: str = ""
    message: str = ""

    @property
    def nights_count(self) -> int:
        return len(self.nights)


def quote_services(booking: Booking, names, now=None, extra_nights=()) -> ServiceQuote:
    """
    Price adding these services to `booking`, at the server's clock.

    A service is delivered on a night, so it is charged for the nights that
    haven't started yet and no others: a guest buying breakfast on the third
    morning of a five-night stay pays for the two mornings still to come, not
    for the five they booked. That is also why a stay with no nights left ahead
    of it refuses the sale outright rather than charging for nothing.

    Unknown names, services the booking already has, and duplicates are all
    dropped rather than refused — the client sends names, the villa's own list
    decides what they cost, and nothing a tampered page sends can invent a
    service or a price.

    `extra_nights` are nights being bought in the SAME breath (see
    `quote_changes`). A guest adding two nights and breakfast in one go is
    buying breakfast for those nights too — quoting the service over the stay as
    it stands would sell them a service that stops before their stay does.
    """
    now = now or timezone.now()
    quote = ServiceQuote()

    blocked = purchase_blocked_reason(booking, now)
    if blocked:
        quote.error = blocked
        return quote

    quote.nights = sorted(set(booking.unstarted_nights(now)) | set(extra_nights))
    if not quote.nights:
        quote.error = MSG_NO_NIGHTS_LEFT
        return quote

    offered = {
        str(s["name"]).strip().lower(): s
        for s in booking.available_extra_services()
    }
    if not offered:
        quote.error = (
            "You already have every extra service this villa offers."
            if booking.live_service_entries()
            else "This villa doesn't offer any extra services."
        )
        return quote

    seen = set()
    nights = len(quote.nights)
    for raw in names or []:
        key = str(raw or "").strip().lower()
        service = offered.get(key)
        if service is None or key in seen:
            continue
        seen.add(key)
        price = _money(service["price"])
        quote.services.append(
            {
                "name": service["name"],
                "price": float(price),
                "nights": nights,
                "amount": float(_money(price * nights)),
            }
        )

    if not quote.services:
        quote.error = "Choose at least one service to add."
        return quote

    quote.amount = _money(sum(Decimal(str(s["amount"])) for s in quote.services))
    quote.allowed = True
    quote.message = (
        f"Charged for the {nights} night{'' if nights == 1 else 's'} of your stay "
        "that haven't started yet."
    )
    return quote


def _stage_services(booking: Booking, quote: ServiceQuote, now) -> list:
    """
    Fold a services purchase into `booking` IN MEMORY, and say which columns it
    touched. Nothing is written — see `apply_changes`, which saves once.

    Services already on the booking are never touched: this only ever appends,
    which is the whole promise of the screen the guest came from.
    """
    entries = booking.service_entries()
    # Backfill the count on services frozen before it existed, so the recompute
    # below prices them over the stay they were actually bought for rather than
    # over whatever the stay has since become.
    for entry in entries:
        entry["nights"] = booking.service_nights(entry)
    for service in quote.services:
        entries.append(
            {
                "name": service["name"],
                "price": service["price"],
                "nights": service["nights"],
                # Which nights these are, so a receipt can say more than a count.
                "added_from": quote.nights[0].isoformat(),
                # And WHICH ONES, named. The count and the start cannot be
                # turned back into these: the nights a service is charged over
                # are the ones still ahead of the guest, and on a split stay —
                # or one with nights already given up — that is not a run. Read
                # back by `Booking.service_billed_night_set`, so a removal hands
                # back exactly the nights this purchase charged for.
                "dates": [n.isoformat() for n in quote.nights],
                "added_at": now.isoformat(),
            }
        )
    booking.extra_services = entries
    booking.extras_total = booking.extras_value()
    booking.total = _money(Decimal(str(booking.total or 0)) + quote.amount)
    return ["extra_services", "extras_total", "total"]


# --------------------------------------------------------------------------
# Extra nights
# --------------------------------------------------------------------------


@dataclass
class NightsQuote:
    """What adding a set of nights to a booking would cost, right now."""

    nights: List[date] = field(default_factory=list)
    # The nights inside the asked-for range the booking ALREADY holds. Not a
    # problem and not charged — the guest simply dragged over their own stay.
    kept: List[date] = field(default_factory=list)
    # The nights inside the range that belong to somebody else. Skipped, not
    # refused: the villa is free either side of them, so the stay simply
    # extends around them — the same bargain checkout strikes (see
    # availability.split_stay).
    skipped: List[date] = field(default_factory=list)
    # Nights inside the range the guest had GIVEN BACK. Left out on purpose: a
    # range never reclaims them, only a tap on the night itself does. Reported
    # so the screen can say they were passed over rather than leave the guest to
    # notice the gap.
    declined: List[date] = field(default_factory=list)
    accommodation: Decimal = Decimal("0.00")
    service_fee: Decimal = Decimal("0.00")
    tax: Decimal = Decimal("0.00")
    # The stay's existing services, carried on over the new nights.
    extras: Decimal = Decimal("0.00")
    services: List[dict] = field(default_factory=list)
    amount: Decimal = Decimal("0.00")
    # What the stay becomes: its new outer dates and how many nights in total.
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    total_nights: int = 0
    allowed: bool = False
    error: str = ""
    message: str = ""

    @property
    def nights_count(self) -> int:
        return len(self.nights)


def _runs(nights) -> list:
    """A set of dates as the unbroken (start, end) runs it makes up."""
    runs, expect = [], None
    for day in sorted(nights):
        if expect is not None and day == expect:
            runs[-1] = (runs[-1][0], day + timedelta(days=1))
        else:
            runs.append((day, day + timedelta(days=1)))
        expect = day + timedelta(days=1)
    return runs


def quote_nights(
    booking: Booking,
    check_in: Optional[date] = None,
    check_out: Optional[date] = None,
    nights=(),
    now=None,
) -> NightsQuote:
    """
    Price adding nights to `booking`, at the server's clock.

    The nights come in two ways, and either or both may be used. `check_in` /
    `check_out` are a RANGE — "extend my stay up to here" — and every free night
    inside it is taken. `nights` is an explicit LIST, which is how a guest picks
    back single nights they had given up.

    The two are unioned, with one subtraction: a night this booking gave back is
    only ever taken when the LIST names it. A range passing over one leaves it
    where it is. Cancelling a night was a decision, and undoing it has to be one
    too — the guest who extends past nights they cancelled is saying where their
    stay now ends, not asking for those nights back by accident.

    The rules are the ones the villa already lives by, asked again for a stay
    that exists: the nights must be inside the host's booking window, free of
    everybody else (this booking's own nights don't count against it), and
    still ahead of the guest — a night that has begun can't be bought.

    A night inside the range that somebody else holds does NOT refuse the
    range — it is skipped, exactly as it is at checkout (see
    availability.split_stay). The villa is genuinely free either side of it, so
    the stay extends around it and only the nights actually slept are charged.

    One rule is this screen's own: what is asked for has to JOIN the stay. It
    may run on from the last night, back from the first, or fill a gap in the
    middle, but it has to touch what is already held — a detached range would be
    a second stay hiding inside the first, and the guest should book that, not
    bolt it on.

    Nothing is charged for nights the guest already holds, so dragging across
    the whole stay to add a day on the end is not a way to pay twice.
    """
    now = now or timezone.now()
    quote = NightsQuote()

    blocked = purchase_blocked_reason(booking, now)
    if blocked:
        quote.error = blocked
        return quote

    villa = booking.villa
    held = booking.occupied_nights()
    given_back = set(booking.cancelled_nights())

    picked = set(nights or ())
    ranged = set()
    if check_in and check_out and check_out > check_in:
        night = check_in
        while night < check_out:
            ranged.add(night)
            night += timedelta(days=1)

    # A night this booking gave back is NOT swept up by a range reaching across
    # it. Giving it up was a decision of its own, and taking it back has to be
    # one too: a guest saying "extend me up to here" past nights they cancelled
    # is saying where their stay now ENDS, not that they have changed their mind
    # about the nights in between. So a given-back night inside a range is left
    # out exactly as a night somebody else holds is — the stay extends around it
    # — and the only way back in is naming it in `nights`, one at a time.
    declined = sorted((ranged & given_back) - picked)
    span = sorted(picked | ranged)
    asked = sorted((picked | ranged).difference(declined))
    if not span:
        quote.error = "Choose the dates you'd like to add."
        return quote
    if not asked:
        quote.error = (
            "Those are nights you gave back. Please tap them one at a time to "
            "take them again."
        )
        return quote

    # What was asked for has to touch the stay: run on from its last night, back
    # from its first, or fill a gap in the middle. Checked on the OUTER BOUNDS of
    # the request rather than on the runs it would produce, because a night
    # somebody else holds — or one given back and left out just above — sits
    # inside it and would otherwise make the rest look like a detached second
    # stay.
    #
    # A night this booking GAVE BACK counts as part of the stay for this test,
    # because it was: taking it back re-joins it. Without that, a guest who
    # dropped the first three nights of their stay could not reclaim the first of
    # them — it would measure as detached from the two that remain, when in fact
    # it is the same booking's own night being picked up again. Only the ones
    # actually named count; a given-back night the range merely passed over is
    # not being taken, so it anchors nothing.
    anchor = held | (picked & given_back)
    span_start = span[0]
    span_end = span[-1] + timedelta(days=1)
    if anchor and (
        span_start > max(anchor) + timedelta(days=1) or span_end < min(anchor)
    ):
        quote.error = (
            "Added nights have to carry on from your stay. Please choose dates "
            "next to the nights you already have."
        )
        return quote

    quote.declined = declined
    quote.kept = [n for n in asked if n in held]
    wanted = [n for n in asked if n not in held]
    if not wanted:
        quote.error = "You already have every night in those dates."
        return quote

    # Still ahead of the guest — a night that has started is being lived in.
    for day in wanted:
        if now >= booking.night_starts_at(day):
            quote.error = (
                f"{_pretty(day)} has already begun, so it can't be added. "
                "Please pick nights that are still ahead."
            )
            return quote

    # Inside the host's rolling window.
    first_open = availability.first_bookable_date(villa, now)
    last_open = availability.last_bookable_date(villa, now)
    for day in wanted:
        if day < first_open:
            quote.error = f"{_pretty(day)} is no longer open for booking."
            return quote
        if day > last_open:
            quote.error = (
                f"This villa is only open for bookings up to {_pretty(last_open)}."
            )
            return quote

    # Free of everybody else. This booking is left out of the count — its own
    # nights are not in its way (see availability.taken_nights).
    taken = availability.taken_nights(
        villa,
        min(wanted),
        max(wanted) + timedelta(days=1),
        for_guest=booking.guest,
        now=now,
        exclude_booking_id=booking.pk,
    )
    # A night somebody else holds is skipped, not refused — the stay extends
    # around it. Only a range with nothing free left in it is a dead end.
    quote.skipped = sorted(taken & set(wanted))
    wanted = [n for n in wanted if n not in taken]
    if not wanted:
        quote.error = (
            "Every night in those dates is already taken. Please choose other dates."
        )
        return quote

    after = _runs(held | set(wanted))
    count = len(wanted)
    quote.nights = wanted
    quote.accommodation = _money(Decimal(str(booking.price_per_night or 0)) * count)
    quote.service_fee = _money(quote.accommodation * SERVICE_FEE_RATE)
    quote.tax = _money(quote.accommodation * TAX_RATE)

    # The services on the stay carry on over the new nights — they are per
    # night, and the guest is not being asked to give them up to stay longer.
    # Only the ones the stay still HAS: a service the guest dropped and was
    # refunded for (see properties/removals.py) must not be sold back to them
    # by the act of staying two nights longer.
    for entry in booking.live_service_entries():
        price = _money(entry.get("price", 0) or 0)
        if price <= 0:
            continue
        quote.services.append(
            {
                "name": str(entry.get("name", "")).strip(),
                "price": float(price),
                "nights": count,
                "amount": float(_money(price * count)),
            }
        )
    quote.extras = _money(sum(Decimal(str(s["amount"])) for s in quote.services))

    quote.amount = _money(
        quote.accommodation + quote.service_fee + quote.tax + quote.extras
    )
    quote.check_in = min(after[0][0], booking.check_in)
    quote.check_out = max(after[-1][1], booking.check_out)
    quote.total_nights = int(booking.nights or 0) + count
    quote.allowed = True
    quote.message = (
        f"{count} night{'' if count == 1 else 's'} added at "
        f"{_money(booking.price_per_night)} a night"
        + (", with your extra services carried on over them." if quote.services else ".")
        + (
            f" {len(quote.skipped)} night"
            f"{' is' if len(quote.skipped) == 1 else 's are'} already taken and "
            "skipped — you check out and back in around them."
            if quote.skipped
            else ""
        )
        + (
            f" {len(quote.declined)} night"
            f"{' you' if len(quote.declined) == 1 else 's you'} gave back "
            f"{'was' if len(quote.declined) == 1 else 'were'} left out — tap "
            "them one at a time to take them again."
            if quote.declined
            else ""
        )
    )
    return quote


def _stage_nights(booking: Booking, quote: NightsQuote) -> list:
    """
    Fold an extension into `booking` IN MEMORY, and say which columns it
    touched. Nothing is written — see `apply_changes`, which saves once.

    The money columns all move together — the stay really is longer, so its
    subtotal, fee, tax and the nights every per-night figure is worked out
    against all grow with it (see `Booking.billed_nights`).
    """
    # Every service the stay still HAS now runs over the new nights too — and
    # is charged for them (see `quote_nights`). One that was given back is left
    # exactly as it is: its billed count is what the guest paid, and it covers
    # nothing in front of them any more.
    #
    # Read BEFORE the stay grows, written after. A service ticked at checkout
    # carries no count and no dates of its own and falls back to the stay's, so
    # asking it what it was billed over once the stay had already been extended
    # would answer with the LONGER stay — and then the new nights would be added
    # to it a second time.
    entries = booking.service_entries()
    live = [e for e in entries if not booking.service_removed(e)]
    frozen = [
        (booking.service_nights(e), booking.service_billed_night_set(e)) for e in live
    ]

    booking.add_nights(quote.nights)
    booking.nights = int(booking.nights or 0) + quote.nights_count

    for entry, (count, dates) in zip(live, frozen):
        entry["nights"] = count + quote.nights_count
        entry["dates"] = [n.isoformat() for n in sorted(dates | set(quote.nights))]
    booking.extra_services = entries

    booking.subtotal = _money(Decimal(str(booking.subtotal or 0)) + quote.accommodation)
    booking.service_fee = _money(
        Decimal(str(booking.service_fee or 0)) + quote.service_fee
    )
    booking.tax = _money(Decimal(str(booking.tax or 0)) + quote.tax)
    booking.extras_total = booking.extras_value()
    booking.total = _money(Decimal(str(booking.total or 0)) + quote.amount)

    return [
        "check_in",
        "check_out",
        "nights",
        "segments",
        "segment_stays",
        "extra_services",
        "subtotal",
        "service_fee",
        "tax",
        "extras_total",
        "total",
    ]


# --------------------------------------------------------------------------
# Both at once
#
# A guest adding two nights AND breakfast is making ONE decision and expects one
# charge for it — not two payments, two receipts and two lines on their card.
# So the two quotes above are only ever halves: what the screen prices and what
# the button performs is the pair of them together.
# --------------------------------------------------------------------------


@dataclass
class ChangesQuote:
    """Everything being added to a booking in one go, and what it comes to."""

    services: Optional[ServiceQuote] = None
    nights: Optional[NightsQuote] = None
    amount: Decimal = Decimal("0.00")
    allowed: bool = False
    error: str = ""
    message: str = ""


def quote_changes(
    booking: Booking, names=(), check_in: Optional[date] = None,
    check_out: Optional[date] = None, nights=(), now=None,
) -> ChangesQuote:
    """
    Price everything the guest is adding, in one answer.

    The nights are quoted first because they change what a service costs: a
    service bought alongside two extra nights runs over those nights too, so it
    is priced over the stay the guest is about to have rather than the one they
    have now. Either half may be empty; if either is refused, the whole thing is
    — a guest who asked for both and can only have one has not been given what
    they asked for, and half a purchase is not the sort of surprise to hand
    somebody at a payment screen.
    """
    now = now or timezone.now()
    combo = ChangesQuote()
    wants_nights = bool((check_in and check_out) or nights)
    wants_services = bool([n for n in (names or []) if str(n or "").strip()])

    if not wants_nights and not wants_services:
        combo.error = "Choose the nights or services you'd like to add."
        return combo

    added_nights = []
    if wants_nights:
        combo.nights = quote_nights(booking, check_in, check_out, nights, now)
        if not combo.nights.allowed:
            combo.error = combo.nights.error
            return combo
        added_nights = combo.nights.nights

    if wants_services:
        combo.services = quote_services(booking, names, now, extra_nights=added_nights)
        if not combo.services.allowed:
            combo.error = combo.services.error
            return combo

    combo.amount = _money(
        (combo.nights.amount if combo.nights else Decimal("0.00"))
        + (combo.services.amount if combo.services else Decimal("0.00"))
    )
    combo.allowed = True
    combo.message = " ".join(
        part.message
        for part in (combo.nights, combo.services)
        if part is not None and part.message
    )
    return combo


def apply_changes(
    booking: Booking, combo: ChangesQuote, *, method: str, reference: str, now=None
) -> BookingAddition:
    """
    Carry out everything `combo` describes and return its single receipt.

    Call inside a transaction; the caller saves nothing else. The nights go in
    first — the services were priced over the longer stay, so the stay has to
    become longer before they are folded in, or the two would disagree about how
    many nights the guest just bought breakfast for.

    One receipt, one payment: the guest made one decision.
    """
    if not combo.allowed:
        raise ValueError(combo.error or "Nothing to add.")
    now = now or timezone.now()

    fields = {"updated_at"}
    if combo.nights is not None:
        fields.update(_stage_nights(booking, combo.nights))
    if combo.services is not None:
        fields.update(_stage_services(booking, combo.services, now))

    nights = combo.nights
    services = combo.services
    record = BookingAddition.objects.create(
        booking=booking,
        kind=(
            BookingAddition.KIND_BOTH
            if nights and services
            else BookingAddition.KIND_NIGHTS if nights
            else BookingAddition.KIND_SERVICES
        ),
        # What was BOUGHT, then what was carried on over the new nights — the
        # `carried` flag is what tells a receipt the two apart, since only the
        # first are services the guest didn't have before.
        services=(services.services if services else [])
        + [{**s, "carried": True} for s in (nights.services if nights else [])],
        nights=[n.isoformat() for n in (nights.nights if nights else services.nights)],
        nights_count=(nights.nights_count if nights else 0),
        accommodation=(nights.accommodation if nights else Decimal("0.00")),
        service_fee=(nights.service_fee if nights else Decimal("0.00")),
        tax=(nights.tax if nights else Decimal("0.00")),
        extras=_money(
            (nights.extras if nights else Decimal("0.00"))
            + (services.amount if services else Decimal("0.00"))
        ),
        amount=combo.amount,
        payment_method=method,
        payment_reference=reference,
        message=combo.message,
    )
    booking.save(update_fields=sorted(fields))
    _forget_additions(booking)
    return record


def _forget_additions(booking: Booking) -> None:
    """
    Drop a prefetched `additions` cache after writing to the relation.

    Same reason `apply_nights_cancellation` drops its own: a booking read with
    `prefetch_related` is holding the rows that existed when it was read, and
    the response this request is about to build would otherwise be answered
    from the state before the addition.
    """
    getattr(booking, "_prefetched_objects_cache", {}).pop("additions", None)
