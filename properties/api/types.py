import logging
from typing import List, Optional

import strawberry

from datetime import date as _date
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Avg
from django.utils import timezone

from accounts.auth import get_user_from_request
from properties import availability, checkin
from properties.models import CheckInVerification

# Same channel the rest of the check-in flow writes to (properties/checkin.py),
# so a no-show recorded here lands in the one audit trail.
checkin_logger = logging.getLogger("properties.checkin")


def _today():
    # The app's own clock, not the host process's — see availability.today_local.
    return availability.today_local()


@strawberry.type
class VillaImageType:
    """One stored villa photo — id + resolvable URL (used when editing)."""

    id: strawberry.ID
    url: str
    # Whether this photo is the villa's cover (host-flagged, position-independent).
    is_cover: bool = False


@strawberry.type
class ExtraServiceType:
    """A premium add-on: a name and its per-night price.

    On a VILLA that is the whole story — the host's list, priced per night. On a
    BOOKING the two below say what was actually bought: how many nights this one
    was charged over and what that came to. A service ticked at checkout ran the
    whole stay; one bought later runs from the night it was bought, so the two
    can't be assumed equal any more (see Booking.service_nights). They are 0 on
    the villa's own list, which has no stay to be charged over.
    """

    name: str
    price: float
    nights: int = 0
    amount: float = 0
    # On an ADDITION's receipt: this service was already on the booking and was
    # carried on over the nights being added, rather than bought in that
    # purchase. False everywhere else (see BookingAddition.services).
    carried: bool = False
    # On a BOOKING's own list: the guest has since dropped this service and been
    # refunded for the nights of it that hadn't started (see
    # properties/removals.py). `nights`/`amount` above still say what it was
    # CHARGED over — that is what the payment column adds up — and `refunded` is
    # how much of it came back. Both are 0 / false everywhere else.
    removed: bool = False
    refunded: float = 0


@strawberry.input
class ExtraServiceInput:
    name: str
    price: float = 0


@strawberry.type
class ReviewType:
    """A guest's rating + written review of a villa, as shown on the listing."""

    id: strawberry.ID
    villa_id: strawberry.ID
    # The villa this review is for — named on the landing-page testimonial cards.
    villa_title: str
    villa_city: str
    rating: int
    comment: str
    author_name: str
    author_avatar: str
    author_gender: str
    created_at: str
    # True when the signed-in viewer wrote this review (so the UI can mark it).
    is_mine: bool

    @classmethod
    def from_model(cls, review, viewer=None) -> "ReviewType":
        guest = review.guest
        villa = review.villa
        return cls(
            id=strawberry.ID(str(review.id)),
            villa_id=strawberry.ID(str(review.villa_id)),
            villa_title=villa.title,
            villa_city=villa.city or villa.country or "",
            rating=int(review.rating),
            comment=review.comment or "",
            author_name=(guest.full_name or guest.email or "Guest").strip(),
            author_avatar=guest.avatar or "",
            author_gender=guest.gender or "",
            created_at=review.created_at.isoformat(),
            is_mine=viewer is not None and viewer.id == review.guest_id,
        )


def _villa_rating(villa) -> float:
    """The villa's average star rating, 1 decimal — from a queryset annotation
    when present (`avg_rating`), else a direct aggregate. 0 when no reviews."""
    avg = getattr(villa, "avg_rating", None)
    if avg is None:
        avg = villa.reviews.aggregate(a=Avg("rating"))["a"]
    return round(float(avg), 1) if avg else 0.0


def _villa_review_count(villa) -> int:
    n = getattr(villa, "review_count", None)
    if n is None:
        n = villa.reviews.count()
    return int(n or 0)


def _clean_extra_services(items) -> List[dict]:
    """
    Normalise an extra-services list to [{"name", "price"}], dropping blanks and
    negatives and de-duplicating by name (case-insensitive, first wins). Shared
    by the villa mutation (host input) and, indirectly, booking snapshots.
    """
    cleaned: List[dict] = []
    seen = set()
    for it in items or []:
        name = (getattr(it, "name", None) if not isinstance(it, dict) else it.get("name")) or ""
        name = str(name).strip()
        if not name or name.lower() in seen:
            continue
        raw_price = getattr(it, "price", None) if not isinstance(it, dict) else it.get("price")
        try:
            price = round(float(raw_price or 0), 2)
        except (TypeError, ValueError):
            price = 0.0
        if price < 0:
            price = 0.0
        seen.add(name.lower())
        cleaned.append({"name": name, "price": price})
    return cleaned


def _hhmm(value) -> str:
    """A TimeField as the "HH:MM" an <input type="time"> round-trips, or ""."""
    return value.strftime("%H:%M") if value else ""


def _pretty_time(value) -> str:
    """14:00 -> "2:00 pm" — how the detail page words a check-in/out time."""
    return value.strftime("%I:%M %p").lstrip("0").lower() if value else ""


def _house_rules(villa) -> List[str]:
    """
    The host's rules, worded for display. Only the times the host actually
    filled in appear; the three permissions always do, since "not allowed" is
    as much an answer as "allowed" and a guest needs to know either way.
    """
    rules = []
    if villa.check_in_time:
        rules.append(f"Check-in: After {_pretty_time(villa.check_in_time)}")
    if villa.check_out_time:
        rules.append(f"Checkout: {_pretty_time(villa.check_out_time)}")
    rules.append("Pets are allowed" if villa.pets_allowed else "Pets are not allowed")
    rules.append(
        "Smoking is allowed" if villa.smoking_allowed else "No smoking"
    )
    rules.append(
        "Events and parties are allowed"
        if villa.events_allowed
        else "No events or parties"
    )
    return rules


def _stamp(value) -> str:
    """An optional datetime as ISO-8601, or "" when it hasn't happened yet."""
    return value.isoformat() if value else ""


@strawberry.type
class StaySegmentType:
    """
    One unbroken run of a stay: arrive, sleep `nights`, leave.

    A stay booked around nights somebody else holds has several of these — the
    guest checks out the morning the other booking starts and back in the
    morning it ends. An ordinary stay has exactly one, matching the booking's
    own check-in/check-out.
    """

    check_in: str
    check_out: str
    nights: int
    # Which part this is, 1-based — so "Part 2 of 3" needs no arithmetic and
    # cannot drift from the order the parts are actually listed in.
    index: int
    # When this part opens and closes, as wall-clock moments at the property.
    # Sent NAIVE (no offset), exactly like the booking's own check_in_at, so the
    # browser shows the hours the host set rather than shifting them into the
    # viewer's time zone — and so they compare directly against `server_now`,
    # which is what the client ticks its progress off.
    check_in_at: str
    check_out_at: str
    # When the guest ACTUALLY arrived for this part and left it, as recorded by
    # the host (PIN-verified arrival, checklist-confirmed departure). "" until
    # each happens. These are real instants, so unlike the scheduled pair above
    # they keep their offset and do localise.
    checked_in_at: str
    checked_out_at: str
    # How many people the host counted in for THIS part (0 until they do). A
    # split stay is arrived at more than once and the party can be a different
    # size each time, so the figure belongs to the part, not to the booking.
    checked_in_guests: int

    @classmethod
    def list_for(cls, booking) -> List["StaySegmentType"]:
        stays = booking.part_stays()
        return [
            cls(
                check_in=start.isoformat(),
                check_out=end.isoformat(),
                nights=(end - start).days,
                index=i,
                check_in_at=starts_at.replace(tzinfo=None).isoformat(),
                check_out_at=ends_at.replace(tzinfo=None).isoformat(),
                checked_in_at=_stamp(stays.get(i, {}).get("checked_in_at")),
                checked_out_at=_stamp(stays.get(i, {}).get("checked_out_at")),
                checked_in_guests=int(stays.get(i, {}).get("guests") or 0),
            )
            for i, (start, end, starts_at, ends_at) in enumerate(
                booking.stay_segment_windows(), start=1
            )
        ]


@strawberry.type
class BookingCancellationType:
    """
    One thing given back off a booking — the receipt for a whole stay called
    off, for the nights of it that were given up, or for an extra service
    dropped (see BookingCancellation).

    A booking may carry several: a guest can drop two nights this week and three
    more later, and each is priced and refunded on the day it happens.
    """

    id: strawberry.ID
    # "full" — the stay was called off — "partial", some of its nights, or
    # "services", an extra service dropped off the nights it hadn't been
    # delivered on yet.
    kind: str
    # The nights given up, ISO dates in order, and how many that is. On a
    # SERVICES row the stay is untouched and keeps every one of them: they are
    # the nights the service was refunded OVER, which is why the count is 0.
    nights: List[str]
    nights_count: int
    # What was dropped, on a services row. Empty on the other two kinds.
    services: List[ExtraServiceType]
    # What those nights were worth of the booking total, and how it split.
    # stay_value = cancellation_fee + refund_amount, always.
    stay_value: float
    cancellation_fee: float
    refund_amount: float
    refund_percentage: int
    # How much of the refund was extra services on those nights — handed back
    # in full, whatever the ladder charged on the stay itself.
    extras_refund: float
    # The policy line the guest was shown as they confirmed it.
    message: str
    created_at: str

    @classmethod
    def list_for(cls, booking) -> List["BookingCancellationType"]:
        return [
            cls(
                id=strawberry.ID(str(row.pk)),
                kind=row.kind,
                nights=[n.isoformat() for n in row.night_dates()],
                nights_count=int(row.nights_count or 0),
                services=[
                    ExtraServiceType(
                        name=str(s.get("name", "")),
                        price=float(s.get("price", 0) or 0),
                        nights=int(s.get("nights", 0) or 0),
                        amount=float(s.get("amount", 0) or 0),
                    )
                    for s in (row.services or [])
                    if isinstance(s, dict) and s.get("name")
                ],
                stay_value=float(row.stay_value or 0),
                cancellation_fee=float(row.cancellation_fee or 0),
                refund_amount=float(row.refund_amount or 0),
                refund_percentage=int(row.refund_percentage or 0),
                extras_refund=float(row.extras_refund or 0),
                message=row.message or "",
                created_at=row.created_at.isoformat(),
            )
            for row in booking.cancellations.all()
        ]


@strawberry.type
class BookingAdditionType:
    """
    One thing added to a booking after it was paid for, and what it cost (see
    BookingAddition) — the mirror of BookingCancellationType.

    A stay can grow more than once, so this is a list too: services bought on
    Tuesday, two more nights on Friday, each with the payment that covered it.
    """

    id: strawberry.ID
    # "services" — extra services were bought — or "nights", the stay grew.
    kind: str
    # What was bought. On a nights purchase `services` are the ones already on
    # the booking, carried on over the new nights.
    services: List[ExtraServiceType]
    nights: List[str]
    nights_count: int
    # The split of what was charged: accommodation + service_fee + tax + extras
    # = amount, always. Only a nights purchase has the first three.
    accommodation: float
    service_fee: float
    tax: float
    extras: float
    amount: float
    # How it was paid for — the method, and the masked account/card.
    payment_method: str
    payment_reference: str
    message: str
    created_at: str

    @classmethod
    def list_for(cls, booking) -> List["BookingAdditionType"]:
        return [
            cls(
                id=strawberry.ID(str(row.pk)),
                kind=row.kind,
                services=[
                    ExtraServiceType(
                        name=str(s.get("name", "")),
                        price=float(s.get("price", 0) or 0),
                        nights=int(s.get("nights", 0) or 0),
                        amount=float(s.get("amount", 0) or 0),
                        carried=bool(s.get("carried")),
                    )
                    for s in (row.services or [])
                    if isinstance(s, dict) and s.get("name")
                ],
                nights=[n.isoformat() for n in row.night_dates()],
                nights_count=int(row.nights_count or 0),
                accommodation=float(row.accommodation or 0),
                service_fee=float(row.service_fee or 0),
                tax=float(row.tax or 0),
                extras=float(row.extras or 0),
                amount=float(row.amount or 0),
                payment_method=row.payment_method or "",
                payment_reference=row.payment_reference or "",
                message=row.message or "",
                created_at=row.created_at.isoformat(),
            )
            for row in booking.additions.all()
        ]


@strawberry.type
class ServiceQuoteType:
    """
    What adding a set of extra services to a booking would cost right now,
    priced by the server with the same code the purchase itself runs (see
    properties/additions.quote_services).

    `allowed` false means it can't go through and `error` says why — so a
    selection the server would refuse comes back as a sentence rather than as a
    button that fails when pressed.
    """

    services: List[ExtraServiceType]
    # The nights every one of them is charged over — the stay's remaining
    # nights, in date order.
    nights: List[str]
    nights_count: int
    amount: float
    allowed: bool
    message: str
    error: str

    @classmethod
    def from_quote(cls, quote) -> "ServiceQuoteType":
        return cls(
            services=[
                ExtraServiceType(
                    name=s["name"],
                    price=float(s["price"]),
                    nights=int(s["nights"]),
                    amount=float(s["amount"]),
                )
                for s in quote.services
            ],
            nights=[n.isoformat() for n in quote.nights],
            nights_count=quote.nights_count,
            amount=float(quote.amount),
            allowed=quote.allowed,
            message=quote.message,
            error=quote.error,
        )


@strawberry.type
class RemovableServiceType:
    """
    One extra service on a booking that can still be dropped, and what dropping
    it would hand back (see properties/removals.removable_services).

    `nights` are the nights of it that HAVEN'T started — the ones coming back,
    in full — and `amount` is what they are worth. `kept` are the nights of it
    already under way: they stay bought and stay charged, because the host has
    delivered them. A service with nothing ahead of it is not listed at all.
    """

    name: str
    price: float
    nights: int
    amount: float
    kept: int


@strawberry.type
class ServiceRemovalQuoteType:
    """
    What dropping a set of extra services would refund right now, priced by the
    server with the same code the removal itself runs (see
    properties/removals.quote_removal).

    The mirror of ServiceQuoteType: `allowed` false means it can't go through
    and `error` says why, so a selection the server would refuse arrives as a
    sentence rather than as a button that fails when pressed.
    """

    services: List[RemovableServiceType]
    # The nights being refunded over, in date order. The stay still holds every
    # one of them — only the service on them is going.
    nights: List[str]
    nights_count: int
    amount: float
    allowed: bool
    message: str
    error: str

    @classmethod
    def from_quote(cls, quote) -> "ServiceRemovalQuoteType":
        return cls(
            services=[
                RemovableServiceType(
                    name=s["name"],
                    price=float(s["price"]),
                    nights=int(s["nights"]),
                    amount=float(s["amount"]),
                    kept=int(s["kept"]),
                )
                for s in quote.services
            ],
            nights=[n.isoformat() for n in quote.nights],
            nights_count=quote.nights_count,
            amount=float(quote.amount),
            allowed=quote.allowed,
            message=quote.message,
            error=quote.error,
        )


@strawberry.type
class NightsQuoteType:
    """
    What extending a booking over a set of dates would cost right now (see
    properties/additions.quote_nights) — the same arithmetic the checkout ran,
    on the nights being added, plus the stay's services carried on over them.

    `kept` are nights inside the chosen range the booking ALREADY holds: not a
    problem and not charged, so dragging across the whole stay to add a day on
    the end never pays for it twice.
    """

    nights: List[str]
    nights_count: int
    kept: List[str]
    # Nights inside the range somebody else holds. Skipped rather than refused,
    # so the stay extends around them — the guest checks out and back in.
    skipped: List[str]
    # Nights inside the range the guest had given back. Passed over on purpose:
    # only a tap on the night itself takes one of those again, never a range.
    declined: List[str]
    accommodation: float
    service_fee: float
    tax: float
    extras: float
    services: List[ExtraServiceType]
    amount: float
    # What the stay would become.
    check_in: str
    check_out: str
    total_nights: int
    allowed: bool
    message: str
    error: str

    @classmethod
    def from_quote(cls, quote) -> "NightsQuoteType":
        return cls(
            nights=[n.isoformat() for n in quote.nights],
            nights_count=quote.nights_count,
            kept=[n.isoformat() for n in quote.kept],
            skipped=[n.isoformat() for n in quote.skipped],
            declined=[n.isoformat() for n in quote.declined],
            accommodation=float(quote.accommodation),
            service_fee=float(quote.service_fee),
            tax=float(quote.tax),
            extras=float(quote.extras),
            services=[
                ExtraServiceType(
                    name=s["name"],
                    price=float(s["price"]),
                    nights=int(s["nights"]),
                    amount=float(s["amount"]),
                )
                for s in quote.services
            ],
            amount=float(quote.amount),
            check_in=quote.check_in.isoformat() if quote.check_in else "",
            check_out=quote.check_out.isoformat() if quote.check_out else "",
            total_nights=quote.total_nights,
            allowed=quote.allowed,
            message=quote.message,
            error=quote.error,
        )


@strawberry.type
class ChangesQuoteType:
    """
    Everything being added to a booking in one go, and what it comes to (see
    properties/additions.quote_changes).

    Both halves are optional and either may be null — what is never optional is
    that they are priced, and paid for, TOGETHER: a guest adding two nights and
    breakfast made one decision, and `amount` is the one figure the button
    charges. If either half can't go through, `allowed` is false and `error`
    says why, rather than half the purchase happening.
    """

    services: Optional[ServiceQuoteType]
    nights: Optional[NightsQuoteType]
    amount: float
    allowed: bool
    message: str
    error: str

    @classmethod
    def from_quote(cls, quote) -> "ChangesQuoteType":
        return cls(
            services=(
                ServiceQuoteType.from_quote(quote.services) if quote.services else None
            ),
            nights=(
                NightsQuoteType.from_quote(quote.nights) if quote.nights else None
            ),
            amount=float(quote.amount),
            allowed=quote.allowed,
            message=quote.message,
            error=quote.error,
        )


@strawberry.type
class BookingEditOptionsType:
    """
    Everything the "edit booking" screen needs to draw itself, in one round
    trip: what may still be added to this stay, what may be dropped from it, and
    the calendar it may be extended over.

    The calendar is this BOOKING's, not the villa's: the nights it already holds
    are not obstacles to it (`unavailable_dates` leaves them out), and `own
    _nights` marks them so the picker can show them as already-yours rather than
    as free dates to buy again.
    """

    booking_id: strawberry.ID
    # The villa's extra services this booking doesn't have yet. Empty means the
    # guest has everything on offer — and the "add a service" door should not be
    # shown at all rather than opening onto an empty list.
    available_services: List[ExtraServiceType]
    # The services this booking HAS that can still be dropped, each with what
    # dropping it would refund. Empty means there is nothing to give back —
    # either the stay has no services, or every night of the ones it has is
    # already under way — and the remove list isn't drawn at all.
    removable_services: List[RemovableServiceType]
    # Whether each door is open, and the one sentence explaining a shut one.
    can_add_services: bool
    can_add_nights: bool
    can_remove_services: bool
    blocked_reason: str
    # The nights a service bought now would be charged over (the stay's nights
    # that haven't started), and the nightly rate a new night would cost.
    chargeable_nights: int
    price_per_night: float
    # The methods this host takes, so an addition can be paid for with one of
    # them rather than only with whatever the booking was paid with.
    accepted_payments: List[str]
    # What the booking was paid with, masked — the "use the same card" option.
    saved_payment_method: str
    saved_payment_reference: str
    # --- The calendar ---
    # The host's rolling window, the dates inside it that somebody else has, and
    # the nights this booking itself holds.
    first_date: str
    last_date: str
    max_check_out: str
    unavailable_dates: List[str]
    own_nights: List[str]
    # Nights this booking gave back and has not taken again. They are free like
    # any other free date — that is what giving them back DID — but the guest
    # who gave them up should read them as theirs to reclaim rather than as
    # strangers' dates, and they sit inside the stay's own span where a range
    # cannot name one without naming its neighbours. So the picker marks them
    # and takes them one at a time (see additions.quote_nights' `nights`).
    cancelled_nights: List[str]
    check_in_time: str
    server_now: str

    @classmethod
    def from_booking(cls, booking, now=None) -> "BookingEditOptionsType":
        from properties import additions as addons, removals

        now = now or timezone.now()
        villa = booking.villa
        # Two questions, not one. `blocked` is why nothing more can be BOUGHT —
        # which a removed listing answers on its own. `drop_blocked` is why
        # nothing can be given BACK, and a removed listing is no answer to that:
        # the guest's refund is still theirs. So a stay on a property the host
        # has taken down opens this screen with the remove list alone.
        blocked = addons.purchase_blocked_reason(booking, now)
        drop_blocked = addons.blocked_reason(booking, now)
        offered = booking.available_extra_services()
        droppable = [] if drop_blocked else removals.removable_services(booking, now)
        chargeable = len(booking.unstarted_nights(now))
        return cls(
            booking_id=strawberry.ID(str(booking.pk)),
            available_services=[
                ExtraServiceType(name=s["name"], price=float(s["price"]))
                for s in offered
            ],
            removable_services=[
                RemovableServiceType(
                    name=s["name"],
                    price=float(s["price"]),
                    nights=int(s["nights"]),
                    amount=float(s["amount"]),
                    kept=int(s["kept"]),
                )
                for s in droppable
            ],
            can_add_services=bool(not blocked and offered and chargeable),
            can_add_nights=not blocked,
            can_remove_services=bool(droppable),
            blocked_reason=blocked,
            chargeable_nights=chargeable,
            price_per_night=float(booking.price_per_night or villa.price_per_night or 0),
            accepted_payments=[
                str(p).strip() for p in (villa.accepted_payments or []) if str(p).strip()
            ],
            saved_payment_method=booking.payment_method or "",
            saved_payment_reference=booking.card_last4 or "",
            first_date=availability.first_bookable_date(villa, now).isoformat(),
            last_date=availability.last_bookable_date(villa, now).isoformat(),
            max_check_out=availability.window_end(villa, now).isoformat(),
            unavailable_dates=[
                d.isoformat()
                for d in availability.unavailable_dates(
                    villa, now, for_guest=booking.guest, exclude_booking_id=booking.pk
                )
            ],
            own_nights=[d.isoformat() for d in sorted(booking.occupied_nights())],
            cancelled_nights=[d.isoformat() for d in booking.cancelled_nights()],
            check_in_time=_hhmm(availability.check_in_cutoff(villa)),
            server_now=timezone.localtime(now).strftime("%Y-%m-%dT%H:%M"),
        )


@strawberry.type
class BookingNightOptionType:
    """
    One night of the stay as the cancel screen sees it (see
    Booking.night_options): what it would hand back if given up right now, or
    why it can't be given up at all.

    Every night the booking was made for is here — the ones still held and the
    ones already cancelled — so the guest is shown their whole stay and reads
    each night's answer on the night itself, before choosing anything.
    """

    # The night slept, ISO date, and which part of the stay it belongs to.
    date: str
    part_index: int
    # "open" — may go right now — or why not: "started" (that part is under
    # way), "expired" (its check-in hour has passed), "cancelled" (already
    # given up). `cancellable` is "open" said plainly for the picker.
    state: str
    cancellable: bool
    # What this ONE night is worth and how it would split at its part's tier —
    # indicative, to label the chip. The binding figure is always the quote for
    # the whole selection, which settles the rounding across the nights picked.
    # On an already-cancelled night these are what it actually got back.
    refund_percentage: int
    stay_value: float
    refund_amount: float
    cancellation_fee: float
    # The policy line for this night, in the guest's words — the free-cancellation
    # notice, the tier's charge, or the reason it can no longer go.
    message: str

    @classmethod
    def list_for(cls, booking, now) -> List["BookingNightOptionType"]:
        return [
            cls(
                date=row["date"].isoformat(),
                part_index=int(row["part_index"]),
                state=row["state"],
                cancellable=bool(row["cancellable"]),
                refund_percentage=int(row["refund_percentage"]),
                stay_value=float(row["stay_value"]),
                refund_amount=float(row["refund_amount"]),
                cancellation_fee=float(row["cancellation_fee"]),
                message=row["message"],
            )
            for row in booking.night_options(now)
        ]


@strawberry.type
class NightsCancellationQuoteType:
    """
    What giving up a chosen set of nights would cost and hand back — the answer
    behind the date picker's live summary, priced by the very same code that
    performs the cancellation (see Booking.nights_cancellation_quote).

    `allowed` False means this selection can't go through, and `error` says why
    in the guest's own words — a middle night, or a part of the stay that has
    already begun.
    """

    nights: List[str]
    nights_count: int
    stay_value: float
    cancellation_fee: float
    refund_amount: float
    refund_percentage: int
    # Of `stay_value`, what was extra services on those nights. Refunded IN
    # FULL, whatever the ladder charges on the stay itself — the host was going
    # to do something on a night that is no longer happening, so there is
    # nothing to keep a percentage of. Already inside `refund_amount`; named
    # separately so the summary can show why the refund beats the tier.
    extras_value: float
    # And the other way: of `stay_value`, the platform fee on those nights. It
    # comes back NOT AT ALL — the fee bought the booking, and the booking
    # happened. Part of `cancellation_fee`, named separately so a refund
    # SMALLER than the tier suggests explains itself too.
    service_fee: float
    # True when the selection is every night still held: this is a whole-stay
    # cancellation, and the UI should say so rather than "3 nights".
    full: bool
    allowed: bool
    message: str
    error: str

    @classmethod
    def from_quote(cls, quote) -> "NightsCancellationQuoteType":
        return cls(
            nights=[n.isoformat() for n in quote.nights],
            nights_count=quote.nights_count,
            stay_value=float(quote.stay_value),
            cancellation_fee=float(quote.penalty_amount),
            refund_amount=float(quote.refund_amount),
            refund_percentage=int(quote.refund_percentage),
            extras_value=float(quote.extras_value),
            service_fee=float(quote.service_fee),
            full=quote.full,
            allowed=quote.allowed,
            message=quote.message,
            error=quote.error,
        )


@strawberry.type
class BookedRangeType:
    """One reservation held on a villa, as its owner sees it."""

    booking_id: strawberry.ID
    check_in: str
    check_out: str
    nights: int
    guests: int
    guest_name: str
    # The runs this stay actually occupies. One entry for an ordinary booking;
    # several when the guest booked around nights that were already taken, in
    # which case check_in/check_out above only bracket them.
    segments: List[StaySegmentType]


@strawberry.type
class VillaAvailabilityType:
    """
    A villa's calendar, for its owner. `booked_dates` is every night already
    taken inside the window — the client draws the calendar straight off it
    instead of re-deriving occupancy from the ranges and getting the half-open
    end date wrong.
    """

    villa_id: strawberry.ID
    window_start: str
    window_end: str
    # The host's own booking window: how many days ahead they're open, and the
    # last date that allows. Editable from the calendar.
    availability_days: int
    bookable_until: str
    is_available_now: bool
    # The date the villa next frees up, when it's occupied today; "" if free.
    free_from: str
    booked_dates: List[str]
    # Nights the host closed by hand — separate from `booked_dates` so the
    # calendar can tell "someone booked this" from "I closed this".
    blocked_dates: List[str]
    upcoming: List[BookedRangeType]
    # The largest party already booked in. Lowering capacity below this would
    # contradict a reservation the host has already accepted.
    max_booked_guests: int


@strawberry.type
class WelcomeOfferType:
    """
    The first-booking welcome offer, as the popup and the payment page read it.

    `available` is what decides whether to advertise it at all — true for a
    signed-out visitor too, since they haven't booked either and the whole point
    is to bring them in. `claimed` separates "you've already had yours" from
    "we don't know who you are yet", so the popup can stop nagging a guest who
    has actually booked.
    """

    available: bool
    claimed: bool
    percent_off: float
    headline: str
    blurb: str
    # True when the viewer is signed in — the popup words its button as "Book
    # now" rather than "Sign in to claim".
    signed_in: bool

    @classmethod
    def for_viewer(cls, user) -> "WelcomeOfferType":
        from properties import welcome

        signed_in = user is not None and getattr(user, "pk", None) is not None
        # A signed-out visitor is shown the offer; only a guest we can see has
        # already booked is told they've used it.
        claimed = signed_in and not welcome.is_eligible(user)
        return cls(
            available=not claimed,
            claimed=claimed,
            percent_off=welcome.percent_off(),
            headline=welcome.HEADLINE,
            blurb=welcome.BLURB,
            signed_in=signed_in,
        )


@strawberry.type
class BookingWindowType:
    """
    The dates a guest may actually pick for one villa — the whole answer the
    reservation calendar needs, in one round trip.

    `first_date` … `last_date` is the host's rolling window (see
    availability.py): `availability_days` consecutive dates starting at the
    first date a guest could still arrive. `unavailable_dates` are the dates
    inside it that are already booked or closed by the host. Anything not in
    the span, or listed in `unavailable_dates`, is disabled on the calendar and
    refused by `createBooking`.
    """

    villa_id: strawberry.ID
    availability_days: int
    # "HH:MM" — the hour that decides whether today is still in the window.
    check_in_time: str
    # The server's own wall clock, "YYYY-MM-DDTHH:MM", when this was answered.
    #
    # The window turns over at the check-in time, and it is the SERVER's clock
    # that decides when — this deployment runs in UTC while a guest may be
    # hours ahead of it. Without this the browser rolls the window over at its
    # own local 2 PM and ends up a whole day out of step with what
    # `createBooking` will accept. The client advances this by however long the
    # page has been open instead of reading its own clock.
    server_now: str
    first_date: str
    last_date: str
    # Exclusive: the latest check-out a stay may have (last_date + 1 day).
    max_check_out: str
    unavailable_dates: List[str]

    @classmethod
    def from_model(cls, villa) -> "BookingWindowType":
        return cls(
            villa_id=strawberry.ID(str(villa.id)),
            availability_days=availability.window_days(villa),
            check_in_time=_hhmm(availability.check_in_cutoff(villa)),
            server_now=timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            first_date=availability.first_bookable_date(villa).isoformat(),
            last_date=availability.last_bookable_date(villa).isoformat(),
            max_check_out=availability.window_end(villa).isoformat(),
            unavailable_dates=[
                d.isoformat() for d in availability.unavailable_dates(villa)
            ],
        )


@strawberry.type
class VillaType:
    id: strawberry.ID
    owner_id: strawberry.ID
    # The host, for the villa detail page's "Hosted by" panel. Avatar is a
    # base64 data-URL (or "" → the frontend draws a gender-based placeholder).
    host_name: str
    host_email: str
    host_phone: str
    host_avatar: str
    host_gender: str
    title: str
    property_type: str
    city: str
    country: str
    address: str
    description: str
    build_up_area: str
    bedrooms: int
    guests: int
    single_bed_rooms: int
    double_bed_rooms: int
    availability_days: int
    bookable_until: str
    # Nights the host has closed by hand, from today forward. Round-trips
    # through the edit form, so the calendar there starts where they left it.
    blocked_dates: List[str]
    services: List[str]
    # Premium add-ons the host offers, each with a per-night price.
    extra_services: List[ExtraServiceType]
    # House rules, twice over: the raw "HH:MM" / booleans the wizard needs to
    # re-populate its own fields when editing, and `house_rules` — the same
    # thing already worded for the detail page, so both sides can't drift.
    check_in_time: str
    check_out_time: str
    # How long past check-in time the host may still check a guest in.
    grace_period_minutes: int
    pets_allowed: bool
    smoking_allowed: bool
    events_allowed: bool
    additional_rules: str
    house_rules: List[str]
    price_per_night: float
    # Live review aggregate: average stars (1 decimal, 0 when none) and count.
    rating: float
    reviews_count: int
    accepted_payments: List[str]
    # Host's bank payout details. payout_account is the MASKED account number.
    payout_account_name: str
    payout_bank_name: str
    payout_ifsc: str
    payout_method: str
    payout_account: str
    images: List[str]
    photos: List[VillaImageType]
    cover_image: str
    created_at: str
    # Availability for the dates and party size the caller asked about. With no
    # dates in the query this still answers for tonight (see availability.py),
    # so a name-only search can flag a villa that can't be stayed in.
    is_available: bool
    unavailable_reason: str
    # Decided on the server from the request's own token, not by the client
    # comparing ids. Every owner-only action is enforced server-side anyway
    # (the mutations filter on `owner=user`); this is what lets the UI offer
    # the action in the first place, off the same source of truth.
    is_owner: bool

    @classmethod
    def from_model(
        cls,
        villa,
        request=None,
        is_available: bool = True,
        unavailable_reason: str = "",
        viewer=None,
    ) -> "VillaType":
        def absolute(url: str) -> str:
            # Local storage returns "/media/..."; Cloudinary returns a full URL.
            if request is not None and url and not url.startswith("http"):
                return request.build_absolute_uri(url)
            return url

        imgs = list(villa.images.all())
        image_urls = [absolute(im.image.url) for im in imgs]
        photos = [
            VillaImageType(
                id=strawberry.ID(str(im.id)),
                url=absolute(im.image.url),
                is_cover=im.is_cover,
            )
            for im in imgs
        ]
        # The cover is the flagged photo (not necessarily the first), else the
        # first — matching Villa.cover_image_url.
        cover = next((p.url for p in photos if p.is_cover), image_urls[0] if image_urls else "")
        owner = villa.owner
        return cls(
            id=strawberry.ID(str(villa.id)),
            owner_id=strawberry.ID(str(villa.owner_id)),
            host_name=(owner.full_name or owner.email or "").strip(),
            host_email=owner.email or "",
            host_phone=owner.phone_number or "",
            host_avatar=owner.avatar or "",
            host_gender=owner.gender or "",
            title=villa.title,
            property_type=villa.property_type,
            city=villa.city,
            country=villa.country,
            address=villa.address,
            description=villa.description,
            build_up_area=villa.build_up_area,
            bedrooms=villa.bedrooms,
            guests=villa.guests,
            single_bed_rooms=villa.single_bed_rooms,
            double_bed_rooms=villa.double_bed_rooms,
            availability_days=villa.availability_days,
            bookable_until=availability.window_end(villa).isoformat(),
            blocked_dates=[
                d.isoformat()
                for d in villa.blocked_dates.filter(
                    date__gte=_today()
                ).values_list("date", flat=True)
            ],
            services=list(villa.services or []),
            extra_services=[
                ExtraServiceType(name=s.get("name", ""), price=float(s.get("price", 0) or 0))
                for s in (villa.extra_services or [])
                if isinstance(s, dict) and s.get("name")
            ],
            check_in_time=_hhmm(villa.check_in_time),
            check_out_time=_hhmm(villa.check_out_time),
            grace_period_minutes=int(villa.grace_period_minutes or 0),
            pets_allowed=villa.pets_allowed,
            smoking_allowed=villa.smoking_allowed,
            events_allowed=villa.events_allowed,
            additional_rules=villa.additional_rules or "",
            house_rules=_house_rules(villa),
            price_per_night=float(villa.price_per_night),
            rating=_villa_rating(villa),
            reviews_count=_villa_review_count(villa),
            accepted_payments=list(villa.accepted_payments or []),
            payout_account_name=villa.payout_account_name,
            payout_bank_name=villa.payout_bank_name,
            payout_ifsc=villa.payout_ifsc,
            payout_method=villa.payout_method,
            payout_account=villa.payout_account,
            images=image_urls,
            photos=photos,
            cover_image=cover,
            created_at=villa.created_at.isoformat(),
            is_available=is_available,
            unavailable_reason=unavailable_reason,
            is_owner=viewer is not None and viewer.id == villa.owner_id,
        )


@strawberry.input
class VillaInput:
    title: str
    property_type: str = ""
    city: str = ""
    country: str = ""
    address: str = ""
    description: str = ""
    build_up_area: str = ""
    bedrooms: int = 1
    guests: int = 1
    single_bed_rooms: int = 0
    double_bed_rooms: int = 0
    availability_days: int = 5
    # Nights the host closed on the calendar. Sent with the rest of the form:
    # nothing on that calendar is saved until the listing itself is.
    blocked_dates: List[str] = strawberry.field(default_factory=list)
    services: List[str] = strawberry.field(default_factory=list)
    # Premium add-ons with a per-night price each.
    extra_services: List[ExtraServiceInput] = strawberry.field(default_factory=list)
    # House rules. Times are "HH:MM" (what <input type="time"> gives); an empty
    # string means the host left it unset.
    check_in_time: str = ""
    check_out_time: str = ""
    # Minutes after check-in time that a late guest can still be checked in;
    # 0 / unset keeps the platform default (see DEFAULT_GRACE_MINUTES).
    grace_period_minutes: int = 0
    pets_allowed: bool = False
    smoking_allowed: bool = False
    events_allowed: bool = False
    additional_rules: str = ""
    price_per_night: float = 0
    accepted_payments: List[str] = strawberry.field(default_factory=list)
    # Host's bank payout details. payout_account is the (full) account number
    # the server masks before storing.
    payout_account_name: str = ""
    payout_bank_name: str = ""
    payout_ifsc: str = ""
    payout_method: str = ""
    payout_account: str = ""
    # Images as base64 data-URLs ("data:image/...;base64,...") from the client.
    images: List[str] = strawberry.field(default_factory=list)
    # Unified display order: each entry is an existing image id or "new" for the
    # next uploaded image. Empty = keep upload order.
    image_order: List[str] = strawberry.field(default_factory=list)
    # Which photo (0-based index into the displayed order) is the cover.
    cover_index: int = 0


@strawberry.type
class BookingType:
    """A guest's reservation, as shown on the 'My Bookings' page."""

    id: strawberry.ID
    villa_id: strawberry.ID
    villa_title: str
    villa_cover: str
    villa_city: str
    villa_country: str
    # --- The listing, when its host has taken it down (Villa.soft_delete) ---
    # The stay itself is untouched by that: it is checked in, checked out,
    # cancelled and reviewed on exactly the terms it was booked on. What changes
    # is how this row READS — the property is no longer somewhere anybody can
    # go and look at, so the title stops being a link, the photo goes grey, and
    # `villa_removed_message` says why in the reader's own terms: the guest is
    # told the property is no longer listed, the host is told THEY removed it.
    villa_removed: bool
    # ISO-8601 instant of the removal, or "" — for "removed on 3 Mar 2026".
    villa_removed_at: str
    villa_removed_message: str
    guest_name: str
    guest_avatar: str
    guest_email: str
    check_in: str
    check_out: str
    nights: int
    # The runs the stay is actually made of. One entry for the ordinary
    # unbroken stay; several when the guest booked around nights somebody else
    # held, in which case check_in/check_out above are only the outer bounds
    # and `nights` counts what was slept, not the days between them.
    segments: List[StaySegmentType]
    guests: int
    # What the host counted through the door, against what the villa sleeps.
    # `checked_in_guests` is 0 until an arrival is verified — `guests` above is
    # only what was booked — and `guest_capacity` is the ceiling the check-in
    # dialog offers and the server enforces (see checkin.headcount).
    checked_in_guests: int
    guest_capacity: int
    price_per_night: float
    subtotal: float
    discount: float
    coupon_code: str
    # What `discount` was for, worded for a receipt: a coupon names its code,
    # the platform's welcome offer names itself. "" when nothing came off.
    discount_label: str
    service_fee: float
    tax: float
    # Extra services the guest chose (name + per-night price) and their summed
    # cost (price × nights). Frozen at booking time, added straight into total.
    # One dropped since is still listed, marked `removed` and carrying what came
    # back: these lines are what was CHARGED, and refunds come off below them.
    extra_services: List[ExtraServiceType]
    extras_total: float
    total: float
    payment_method: str
    card_last4: str
    status: str
    # ISO-8601 timestamps of when the host marked the guest in / out, or "".
    checked_in_at: str
    checked_out_at: str
    created_at: str
    # --- The host, shown to the GUEST on their own booking's detail panel ---
    host_name: str
    host_email: str
    host_phone: str
    host_avatar: str
    host_gender: str
    # The guest's own contact phone, shown to the HOST on the rent-request panel.
    guest_phone: str
    # Scheduled start/end datetimes (the villa's check-in/out time on those
    # dates), ISO-8601. What "late" / "no-show" are measured against.
    check_in_at: str
    check_out_at: str
    # The server's own wall clock when this was answered, same naive form as
    # check_in_at/check_out_at so the three compare directly.
    #
    # The host's check-in button opens at an exact hour, and it is the SERVER
    # that decides when — the browser may sit in another time zone (this
    # deployment runs in UTC), so a button judged on the browser's clock would
    # unlock hours early or late. The page advances this stamp by however long
    # it has been open instead of reading its own clock.
    server_now: str
    # Derived lifecycle: upcoming / awaiting_checkin / staying / completed /
    # no_show / cancelled — computed live from the clock (see Booking).
    lifecycle_status: str
    # Hours past the scheduled check-in with the guest still not checked in
    # (0 unless awaiting_checkin) — drives the "X hrs late" text.
    hours_late: float
    # The flexible cancellation policy, read at `server_now` (see Booking.
    # cancellation_policy): whether cancelling is still open, the split a
    # cancellation right now would carry, and the line to show the guest —
    # "Free cancellation available." / the 24-hour non-refundable notice /
    # "Cancellation period has expired."
    #
    # On a CANCELLED booking these describe what actually happened, and
    # `cancellation_fee` / `refund_amount` are the frozen amounts; on a live one
    # `cancellation_fee` is 0 and `cancel_fee_now` / `refund_amount_now` are the
    # hypothetical split.
    can_cancel: bool
    refund_percentage: int
    penalty_percentage: int
    cancellation_message: str
    cancel_fee_now: float
    refund_amount_now: float
    cancellation_fee: float
    refund_amount: float

    # --- Giving up part of a stay (see Booking.nights_cancellation_quote) ---
    # `cancellable_nights` are the nights the guest may still choose to drop:
    # every night of a part nobody has arrived for and whose check-in hour
    # hasn't passed. Empty once there is nothing left to give up, which is what
    # hides the "selected dates" option rather than offering an empty picker.
    #
    # `cancelled_nights` are the ones already given up, `active_nights` is what
    # the guest is still coming for, and `cancellations` is the receipt for each
    # event — with `refunded_total` their sum. A partially cancelled booking is
    # still ACTIVE: the stay goes ahead, shorter.
    cancellable_nights: List[str]
    cancelled_nights: List[str]
    # The same stay laid out night by night, each with what cancelling it right
    # now would hand back — or why it can't go (see Booking.night_options).
    # This is what the guest's cancel screen draws: every booked date visible,
    # the ones that can't be cancelled disabled and carrying their reason.
    night_options: List[BookingNightOptionType]
    active_nights: int
    partially_cancelled: bool
    cancellations: List[BookingCancellationType]
    refunded_total: float

    # --- Growing a stay that's already paid for (properties/additions.py) ---
    # The mirror of the block above: a booking can be added to as well as cut
    # back. `available_services` are the villa's add-ons this booking doesn't
    # have yet — empty is what HIDES the "add a service" option rather than
    # opening an empty picker — and the two flags say whether either door is
    # open at all (both shut on a cancelled or finished stay).
    available_services: List[ExtraServiceType]
    can_add_services: bool
    can_add_nights: bool
    # One receipt per purchase made after booking, oldest first, and their sum.
    additions: List[BookingAdditionType]
    additions_total: float

    # --- Check-in window (see Booking.check_in_gate) ---
    # The host's button, as the server sees it: whether it shows at all, what
    # colour it is ("grey" before the hour, "green" inside the window, "yellow"
    # in its closing stretch, "hidden" once it's gone), whether pressing it is
    # allowed, and how many minutes of the grace period are left.
    booking_status: str
    checkin_available: bool
    button_state: str
    button_visible: bool
    grace_period_remaining_minutes: int
    otp_required: bool
    checkin_message: str
    # When the window opens and when it shuts — naive wall-clock, like
    # check_in_at, so they compare to server_now directly.
    grace_ends_at: str
    # Stamped when the window closed with nobody checked in ("" until then).
    # That moment also cancels the booking outright, so this is what tells the
    # two states apart: a stay cancelled with a no_show_at was not called off by
    # anybody, it simply ran out of guest.
    no_show_at: str
    # What to SAY about it, worded for whoever is reading — the host is told
    # about their calendar, the guest about their money. "" when it isn't one.
    no_show_message: str

    # --- Check-out window (see Booking.check_out_gate) ---
    # There is no hour to miss on the way out, so this is only "may the host
    # close this stay right now", plus the line both sides are shown about what
    # leaving at THIS moment would mean — including, while the guest is still
    # in, that it would be early and how many nights that gives back.
    checkout_available: bool
    checkout_message: str
    # Whether closing the stay at this moment would be an EARLY departure. What
    # turns `checkout_message` from a reminder into a warning the host should
    # read before they verify the PIN.
    checkout_early_now: bool
    # And what a departure actually did: leaving before the booked check-out
    # hands the unused nights back to the calendar and refunds nothing. Both are
    # 0 / false until it happens.
    early_check_out: bool
    released_nights: int

    # --- Forced check-out (see checkin.sync_forced_check_out) ---
    # The stay closes itself half an hour after the hour the guest had to be
    # out. `checkout_overdue` is that half hour running: the hour has passed
    # with nobody checked out. `auto_check_out_at` is when it closes — naive
    # wall-clock, like check_out_at, so it compares directly to `server_now` —
    # and `auto_check_out_seconds_left` is the same thing already counted, so
    # neither side has to do date arithmetic to draw the countdown. Both are
    # "" / 0 whenever there is nothing pending.
    #
    # `forced_check_out` says it has already happened: this departure was the
    # platform's, on the clock, with no PIN — which is a different fact about
    # the stay than `checked_out_at`, and the one a disputed departure turns on.
    checkout_overdue: bool
    auto_check_out_at: str
    auto_check_out_seconds_left: int
    forced_check_out: bool
    forced_check_out_at: str

    # --- Stay PINs ---
    # `checkin_pin` / `checkout_pin` — the digits — are filled in for the GUEST
    # ALONE, and only while a PIN is live. The host reading the very same booking
    # gets "": the whole point of the code is that they have to be told it.
    #
    # The other three of each are NOT secret and go to both sides: how long the
    # code has left, that one is waiting to be typed, and how many tries remain.
    # The host needs all three to run the conversation ("it's expired, ask me for
    # a new one"), and none of them helps anyone guess the digits.
    checkin_pin: str
    checkin_pin_expires_in: int
    checkin_pin_pending: bool
    checkin_pin_attempts_left: int
    checkout_pin: str
    checkout_pin_expires_in: int
    checkout_pin_pending: bool
    checkout_pin_attempts_left: int

    # Reviews: whether the guest may review this stay (completed + not yet
    # reviewed), and their review if they've already left one (0 / "" if not).
    can_review: bool
    review_rating: int
    review_comment: str
    # When the review was posted (ISO instant, "" when there isn't one) — shown
    # to guest and host alike, so both are reading the same dated statement.
    review_created_at: str
    # Whether the guest may still change it: editing closes 24 hours after
    # posting (Review.EDIT_WINDOW_HOURS), and the Edit control goes with it.
    can_edit_review: bool
    review_editable_until: str

    @classmethod
    def from_model(cls, booking, request=None) -> "BookingType":
        villa = booking.villa
        guest = booking.guest
        owner = villa.owner
        # One clock reading drives every derived value below, so the lifecycle,
        # "hours late" and the live cancellation fee can't disagree with each
        # other by the odd millisecond.
        now = timezone.now()
        policy = booking.cancellation_policy(now)
        # Reading a booking is also when a no-show is acted on: nothing is
        # waiting on the stroke of the hour, so there's no scheduler — the first
        # look after the window shut stamps the moment AND cancels the stay for
        # nothing back (see sync_no_show). The calendar does the same looking,
        # so this is not the only door it can happen behind.
        if booking.sync_no_show(now):
            checkin_logger.info(
                "checkin event=no_show_cancelled booking=%s villa=%s guest=%s at=%s",
                booking.pk, booking.villa_id, booking.guest_id,
                booking.no_show_at.isoformat(),
            )
        # And the other end of the stay, on the same terms: a departure nobody
        # recorded is closed here, half an hour past the hour the guest had to
        # be out (see checkin.sync_forced_check_out). BEFORE the gates below are
        # read, so this booking is described as the closed stay it now is
        # rather than as one still waiting on a PIN.
        checkin.sync_forced_check_out(booking, now=now)
        # Read after that sync, so a stay just closed reports no countdown.
        auto_out_at = booking.auto_check_out_at(now)
        gate = booking.check_in_gate(now)
        out_gate = booking.check_out_gate(now)
        cancelled = booking.status == booking.STATUS_CANCELLED
        stored_fee = float(booking.cancellation_fee or 0)
        # Read once: the nights still up for giving up, the ones already given
        # up, and the receipts. All three come off the same rows, and the picker
        # the guest sees is drawn from exactly what the server would accept.
        cancel_rows = list(booking.cancellations.all())
        cancelled_nights = booking.cancelled_nights()
        cancellable_nights = booking.cancellable_nights(now)
        # And the same for the other direction: what has been ADDED since, and
        # whether anything more may be. A stay can only grow while it still has
        # nights ahead of it and a listing to buy them on — see properties/
        # additions.purchase_blocked_reason, which is what the mutations enforce
        # and this only mirrors.
        addition_rows = list(booking.additions.all())
        from properties import additions as addons

        unstarted = booking.unstarted_nights(now)
        can_add = not cancelled and not addons.purchase_blocked_reason(booking, now)

        # The live check-in PIN, if there is one. Which of its fields are filled
        # in depends entirely on who is asking: the digits go to the guest and
        # to nobody else, so a host inspecting the raw API response of their own
        # booking finds the same "" the UI shows them.
        # NOT `request.user`: this app decodes its JWT by hand and never writes
        # it back there, so Django's `request.user` is anonymous even for a
        # signed-in guest — which would hide every guest's own PIN from them.
        viewer = get_user_from_request(request)
        is_guest = viewer is not None and viewer.pk == booking.guest_id
        # Only worth a query while the stay is actually at that point: a PIN
        # can't be issued outside it, and one issued a moment before the window
        # shut is refused at verification anyway.
        pin_row = (
            CheckInVerification.live_for(booking, now, purpose=CheckInVerification.PURPOSE_CHECK_IN)
            if gate.otp_required
            else None
        )
        # Past the booked hour a departure takes no code at all, so the guest is
        # shown none — a PIN issued in the last minute before that hour would
        # otherwise sit on their booking page for another minute, asking to be
        # read out for a step the host no longer has to take.
        out_pin_row = (
            CheckInVerification.live_for(booking, now, purpose=CheckInVerification.PURPOSE_CHECK_OUT)
            if out_gate.available and booking.check_out_pin_required(now)
            else None
        )
        # The guest's review of this stay, if any (reverse one-to-one).
        try:
            review = booking.review
        except ObjectDoesNotExist:
            review = None
        # A stay can be reviewed once it is genuinely over, isn't cancelled, and
        # hasn't been reviewed yet.
        #
        # `stay_finished` — every PART checked out — not `checked_out_at`, which
        # answers a different question on a split stay. A stay that was booked
        # unbroken, stayed, and closed out carries that stamp forever; if nights
        # in the middle are later taken by somebody else the same booking becomes
        # two parts, and the guest is due back for the second one with the old
        # stamp still sitting there. That guest was being offered "Rate stay" on
        # a stay they hadn't taken yet.
        # `any_part_arrived` as well as finished: the parts also run out on a
        # guest who never turned up, and a no-show has no stay to rate.
        can_review = (
            booking.stay_finished
            and booking.any_part_arrived
            and not cancelled
            and review is None
        )

        def absolute(url: str) -> str:
            # Only local media paths ("/media/...") need the host prefix;
            # full URLs and data-URLs (avatars) pass through untouched.
            if request is not None and url and url.startswith("/"):
                return request.build_absolute_uri(url)
            return url

        return cls(
            id=strawberry.ID(str(booking.id)),
            villa_id=strawberry.ID(str(villa.id)),
            # Frozen villa identity, so a booking's shown property can't change
            # under the guest when the host edits the listing. Legacy bookings
            # (blank snapshot) fall back to the villa's current values.
            villa_title=booking.villa_title or villa.title,
            villa_cover=absolute(villa.cover_image_url),
            villa_city=booking.villa_city or villa.city,
            villa_country=booking.villa_country or villa.country,
            # Worded for whoever is reading: the host who took the listing down
            # is told so as something they did, everyone else is told the
            # property is simply no longer listed. `viewer` is read the same way
            # the PIN fields above read it — from the JWT, not request.user.
            villa_removed=villa.is_deleted,
            villa_removed_at=(
                villa.deleted_at.isoformat() if villa.deleted_at else ""
            ),
            villa_removed_message=villa.removal_message(
                for_owner=viewer is not None and viewer.pk == villa.owner_id
            ),
            guest_name=(guest.full_name or guest.email or "").strip(),
            guest_avatar=guest.avatar or "",
            guest_email=guest.email or "",
            check_in=booking.check_in.isoformat(),
            check_out=booking.check_out.isoformat(),
            nights=booking.nights,
            segments=StaySegmentType.list_for(booking),
            guests=booking.guests,
            checked_in_guests=int(booking.checked_in_guests or 0),
            guest_capacity=booking.guest_capacity(),
            price_per_night=float(booking.price_per_night),
            subtotal=float(booking.subtotal),
            discount=float(booking.discount),
            coupon_code=booking.coupon_code or "",
            discount_label=(
                "First booking offer"
                if booking.first_booking_discount
                else (booking.coupon_code or "")
            ),
            service_fee=float(booking.service_fee),
            tax=float(booking.tax),
            extra_services=[
                ExtraServiceType(
                    name=s.get("name", ""),
                    price=float(s.get("price", 0) or 0),
                    # What this one was actually charged over. A service ticked
                    # at checkout ran the whole stay; one bought later runs from
                    # the night it was bought (see Booking.service_nights).
                    nights=booking.service_nights(s),
                    amount=float(
                        (Decimal(str(s.get("price", 0) or 0)) * booking.service_nights(s)).quantize(
                            Decimal("0.01")
                        )
                    ),
                    # And what came back off it, on a service since dropped. The
                    # two above stay the CHARGE — the payment column adds them
                    # up, and the refund comes off at the foot of it with every
                    # other refund — so this only marks the line.
                    removed=booking.service_removed(s),
                    refunded=float(booking.service_refunded_value(s)),
                )
                for s in (booking.extra_services or [])
                if isinstance(s, dict) and s.get("name")
            ],
            extras_total=float(booking.extras_total),
            total=float(booking.total),
            payment_method=booking.payment_method,
            card_last4=booking.card_last4,
            status=booking.status,
            checked_in_at=booking.checked_in_at.isoformat() if booking.checked_in_at else "",
            checked_out_at=booking.checked_out_at.isoformat() if booking.checked_out_at else "",
            created_at=booking.created_at.isoformat(),
            host_name=(owner.full_name or owner.email or "").strip(),
            host_email=owner.email or "",
            host_phone=owner.phone_number or "",
            host_avatar=owner.avatar or "",
            host_gender=owner.gender or "",
            guest_phone=(booking.contact_phone or guest.phone_number or "").strip(),
            # The SCHEDULED check-in/out are wall-clock times at the property
            # (2 PM / 11 AM), not moments in a timezone — sent naive (no offset)
            # so the browser shows them exactly as set, never shifted to the
            # viewer's local time. (checked_in_at/out_at above are real instants
            # and stay tz-aware, so they DO localise — that's correct for them.)
            #
            # These follow the PART in front of us, which on an unbroken stay
            # is the stay itself. That is what makes the check-in button open on
            # the right hour after a split stay's first part is closed off: the
            # window it counts down to is part two's, not the booking's.
            check_in_at=booking.current_part_opens_at().replace(tzinfo=None).isoformat(),
            check_out_at=booking.current_part_ends_at().replace(tzinfo=None).isoformat(),
            server_now=timezone.localtime(now).replace(tzinfo=None).isoformat(
                timespec="seconds"
            ),
            lifecycle_status=booking.lifecycle_status(now),
            hours_late=booking.hours_late(now),
            can_cancel=policy.can_cancel,
            refund_percentage=policy.refund_percentage,
            penalty_percentage=policy.penalty_percentage,
            cancellation_message=policy.message,
            cancel_fee_now=float(policy.penalty_amount),
            refund_amount_now=float(policy.refund_amount),
            cancellation_fee=stored_fee,
            # What actually went back. Once there are cancellation rows they are
            # the authority — a booking trimmed twice was refunded twice, and
            # only they know what each of those was worth. The subtraction is
            # the fallback for stays cancelled before the rows existed.
            refund_amount=(
                float(booking.refunded_total())
                if cancel_rows
                else (float(booking.total) - stored_fee) if cancelled else 0.0
            ),
            cancellable_nights=[n.isoformat() for n in cancellable_nights],
            cancelled_nights=[n.isoformat() for n in cancelled_nights],
            night_options=BookingNightOptionType.list_for(booking, now),
            active_nights=len(booking.occupied_nights()) if not cancelled else 0,
            partially_cancelled=bool(cancelled_nights) and not cancelled,
            cancellations=BookingCancellationType.list_for(booking),
            refunded_total=float(booking.refunded_total()),
            # What may still be added, decided the same way the mutation will
            # decide it — a shut door is never drawn as an open one. Both are
            # false on a cancelled stay and on one every part of which is over.
            available_services=[
                ExtraServiceType(name=s["name"], price=float(s["price"]))
                for s in (booking.available_extra_services() if can_add else [])
            ],
            can_add_services=bool(
                can_add and booking.available_extra_services() and unstarted
            ),
            can_add_nights=can_add,
            additions=BookingAdditionType.list_for(booking),
            additions_total=float(
                sum((Decimal(str(r.amount or 0)) for r in addition_rows), Decimal("0"))
            ),
            booking_status=gate.booking_status,
            checkin_available=gate.checkin_available,
            button_state=gate.button_state,
            button_visible=gate.button_visible,
            grace_period_remaining_minutes=gate.grace_period_remaining_minutes,
            otp_required=gate.otp_required,
            checkin_message=gate.message,
            grace_ends_at=gate.grace_ends_at.replace(tzinfo=None).isoformat(),
            no_show_at=booking.no_show_at.isoformat() if booking.no_show_at else "",
            # Read the same way `villa_removed_message` is: off the JWT's
            # viewer, so the host reads the host's sentence and everyone else
            # reads the guest's.
            no_show_message=booking.no_show_message(
                for_owner=viewer is not None and viewer.pk == villa.owner_id
            ),
            checkout_available=out_gate.available,
            checkout_message=out_gate.message,
            checkout_early_now=out_gate.early,
            # What actually happened, never what would: `checkout_message` is
            # the "if they left now" half, and these two are the record of a
            # departure that already took place.
            early_check_out=booking.released_nights > 0,
            released_nights=int(booking.released_nights or 0),
            # The overrun, and the clock on it. `auto_check_out_at` is naive
            # like the scheduled hours above — it is a time at the PROPERTY, and
            # a browser in another zone must not shift it.
            checkout_overdue=booking.check_out_overdue(now),
            auto_check_out_at=(
                timezone.localtime(auto_out_at).replace(tzinfo=None).isoformat()
                if auto_out_at
                else ""
            ),
            auto_check_out_seconds_left=booking.auto_check_out_seconds_left(now),
            forced_check_out=booking.forced_check_out_at is not None,
            forced_check_out_at=(
                booking.forced_check_out_at.isoformat()
                if booking.forced_check_out_at
                else ""
            ),
            # The digits, to the guest alone (see the field comments above).
            checkin_pin=(pin_row.pin if pin_row and is_guest else ""),
            checkin_pin_expires_in=(pin_row.seconds_left(now) if pin_row else 0),
            checkin_pin_pending=pin_row is not None,
            checkin_pin_attempts_left=(
                pin_row.attempts_left if pin_row else CheckInVerification.MAX_FAILED_ATTEMPTS
            ),
            checkout_pin=(out_pin_row.pin if out_pin_row and is_guest else ""),
            checkout_pin_expires_in=(out_pin_row.seconds_left(now) if out_pin_row else 0),
            checkout_pin_pending=out_pin_row is not None,
            checkout_pin_attempts_left=(
                out_pin_row.attempts_left
                if out_pin_row
                else CheckInVerification.MAX_FAILED_ATTEMPTS
            ),
            can_review=can_review,
            review_rating=int(review.rating) if review else 0,
            review_comment=(review.comment or "") if review else "",
            review_created_at=(
                review.created_at.isoformat() if review and review.created_at else ""
            ),
            can_edit_review=bool(review and review.can_edit(now)),
            review_editable_until=(
                review.editable_until().isoformat()
                if review and review.created_at
                else ""
            ),
        )


@strawberry.type
class SavedPaymentType:
    """
    A way this guest has paid before, offered back to them at checkout.

    Only ever the MASKED reference and the billing address that went with it —
    the card number and CVV were never stored in the first place (see
    mutations._mask_account), so there is nothing here to leak and nothing to
    re-enter. Choosing one is choosing "the same as last time"; the server looks
    the details up again from the guest's own bookings rather than trusting
    anything the page sends back.
    """

    method: str
    reference: str
    billing_street: str
    billing_apartment: str
    billing_city: str
    billing_state: str
    billing_zip: str
    billing_country: str
    # When it was last used, ISO — what orders the list.
    last_used: str

    @classmethod
    def list_for(cls, user) -> List["SavedPaymentType"]:
        from properties.models import Booking as _Booking

        rows = (
            _Booking.objects.filter(guest=user)
            .exclude(payment_method="")
            .exclude(card_last4="")
            .order_by("-created_at")
        )
        out, seen = [], set()
        for row in rows:
            key = (row.payment_method, row.card_last4)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                cls(
                    method=row.payment_method,
                    reference=row.card_last4,
                    billing_street=row.billing_street or "",
                    billing_apartment=row.billing_apartment or "",
                    billing_city=row.billing_city or "",
                    billing_state=row.billing_state or "",
                    billing_zip=row.billing_zip or "",
                    billing_country=row.billing_country or "",
                    last_used=row.created_at.isoformat(),
                )
            )
        return out


@strawberry.input
class AddonPaymentInput:
    """
    How an addition to an existing booking is paid for.

    Same shape as the payment half of `BookingInput`, because it is the same
    payment: the host's accepted methods, a card with its billing address or an
    account handle, validated by the same code (see mutations._resolve_payment).

    `use_saved` is the one thing checkout has no need of — the guest already
    paid for this stay once, and paying for an addition the same way should not
    mean typing a card number again. It reuses the booking's own method and its
    masked reference, and nothing else on this input is read.
    """

    use_saved: bool = False
    payment_method: str = ""
    card_number: str = ""
    expiration: str = ""
    cvv: str = ""
    payment_detail: str = ""
    billing_street: str = ""
    billing_apartment: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_zip: str = ""
    billing_country: str = ""


@strawberry.input
class BookingInput:
    villa_id: strawberry.ID
    check_in: str  # ISO date "YYYY-MM-DD"
    check_out: str  # ISO date "YYYY-MM-DD"
    guests: int = 1
    # How many nights the page priced before the guest typed a card number.
    # A range can contain nights somebody else holds — those are skipped and
    # not charged — so the count is not simply check_out minus check_in, and if
    # availability moves mid-checkout the server re-quotes rather than charging
    # for a stay the guest never saw. 0 = not sent (older clients).
    expected_nights: int = 0
    payment_method: str = ""
    card_number: str = ""
    expiration: str = ""
    cvv: str = ""  # validated for shape, never stored
    # PayPal / Google Pay account the guest pays from (e-mail or UPI id). Masked
    # before storage, same as the card. Empty for card payments.
    payment_detail: str = ""
    billing_street: str = ""
    billing_apartment: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_zip: str = ""
    billing_country: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    # Optional discount code. Re-validated and applied on the server; an invalid
    # or non-applicable code fails the booking rather than silently costing full.
    coupon_code: str = ""
    # Pay the way this guest paid before: the method named above is looked up
    # against their own past bookings and its masked reference and billing
    # address reused, so they don't retype a card the platform never kept
    # anyway. The card fields above are ignored when this is set.
    use_saved_payment: bool = False
    # Names of the extra services the guest ticked. Prices are looked up from the
    # villa on the server (never trusted from the client).
    extra_services: List[str] = strawberry.field(default_factory=list)


@strawberry.type
class CouponType:
    """A discount code as its owner (the host) manages it on the Coupons page."""

    id: strawberry.ID
    # "" for a common coupon (applies to every villa the host owns), else the id.
    villa_id: str
    villa_title: str
    code: str
    discount_type: str  # "percent" | "fixed"
    discount_value: float
    active: bool
    # "all" (every villa the host owns) or "villa" (one specific listing).
    scope: str
    # A short human label for the value, e.g. "20% off" / "$50 off".
    label: str
    created_at: str
    # --- Validity period (both optional, "YYYY-MM-DD" or "") ---
    # Inclusive at both ends; "" means unset (starts now / never expires).
    valid_from: str
    valid_until: str
    # "active" | "scheduled" | "expired" | "inactive" — computed on the server,
    # on the server's own date. The client must NOT re-derive this: the backend
    # runs in UTC and a browser hours ahead of it would disagree about which day
    # it is, and label a live coupon expired.
    status: str
    # The period in one line ("Valid until 30 Sep 2026"), "" when it has none.
    validity_label: str
    # Whole days left including today; -1 when the coupon never expires.
    days_left: int

    @classmethod
    def from_model(cls, coupon) -> "CouponType":
        from properties import coupons as coupon_utils

        return cls(
            id=strawberry.ID(str(coupon.id)),
            villa_id=str(coupon.villa_id) if coupon.villa_id else "",
            villa_title=coupon.villa.title if coupon.villa_id else "",
            code=coupon.code,
            discount_type=coupon.discount_type,
            discount_value=float(coupon.discount_value),
            active=coupon.active,
            scope="villa" if coupon.villa_id else "all",
            label=coupon_utils.label_for(coupon),
            created_at=coupon.created_at.isoformat(),
            valid_from=coupon.valid_from.isoformat() if coupon.valid_from else "",
            valid_until=coupon.valid_until.isoformat() if coupon.valid_until else "",
            status=coupon.status_on(),
            validity_label=coupon_utils.validity_label(coupon),
            days_left=coupon_utils.days_left(coupon),
        )


@strawberry.input
class CouponInput:
    # "" = a common coupon covering all the host's villas; else a villa id.
    villa_id: str = ""
    code: str = ""
    discount_type: str = "percent"
    discount_value: float = 0
    active: bool = True
    # Validity period, "YYYY-MM-DD". Both optional: "" for valid_from means it
    # works right away, "" for valid_until means it never expires.
    valid_from: str = ""
    valid_until: str = ""


@strawberry.type
class OfferType:
    """
    A live offer for the landing page: a real villa paired with a coupon that
    applies to it. Drives the on-load coupon popup and the promo images.
    """

    villa_id: strawberry.ID
    title: str
    city: str
    country: str
    cover_image: str
    price_per_night: float
    code: str
    discount_type: str
    discount_value: float
    label: str

    @classmethod
    def from_pair(cls, villa, coupon, request=None) -> "OfferType":
        from properties import coupons as coupon_utils

        url = villa.cover_image_url
        if request is not None and url and not url.startswith("http"):
            url = request.build_absolute_uri(url)
        return cls(
            villa_id=strawberry.ID(str(villa.id)),
            title=villa.title,
            city=villa.city,
            country=villa.country,
            cover_image=url,
            price_per_night=float(villa.price_per_night),
            code=coupon.code,
            discount_type=coupon.discount_type,
            discount_value=float(coupon.discount_value),
            label=coupon_utils.label_for(coupon),
        )


@strawberry.type
class CouponPreviewType:
    """
    The payment page's live check on a code the guest typed. `valid` says
    whether it applies; when it does, `discount` is the amount off this stay.
    """

    valid: bool
    message: str
    code: str
    discount_type: str
    discount_value: float
    discount: float
    label: str
