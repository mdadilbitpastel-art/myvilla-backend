from typing import List

import strawberry

from datetime import date as _date

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Avg
from django.utils import timezone

from properties import availability


def _today():
    return _date.today()


@strawberry.type
class VillaImageType:
    """One stored villa photo — id + resolvable URL (used when editing)."""

    id: strawberry.ID
    url: str


@strawberry.type
class ExtraServiceType:
    """A premium add-on: a name and its per-night price."""

    name: str
    price: float


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


@strawberry.type
class BookedRangeType:
    """One reservation held on a villa, as its owner sees it."""

    booking_id: strawberry.ID
    check_in: str
    check_out: str
    nights: int
    guests: int
    guest_name: str


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
            VillaImageType(id=strawberry.ID(str(im.id)), url=absolute(im.image.url))
            for im in imgs
        ]
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
            pets_allowed=villa.pets_allowed,
            smoking_allowed=villa.smoking_allowed,
            events_allowed=villa.events_allowed,
            additional_rules=villa.additional_rules or "",
            house_rules=_house_rules(villa),
            price_per_night=float(villa.price_per_night),
            rating=_villa_rating(villa),
            reviews_count=_villa_review_count(villa),
            accepted_payments=list(villa.accepted_payments or []),
            payout_method=villa.payout_method,
            payout_account=villa.payout_account,
            images=image_urls,
            photos=photos,
            cover_image=image_urls[0] if image_urls else "",
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
    pets_allowed: bool = False
    smoking_allowed: bool = False
    events_allowed: bool = False
    additional_rules: str = ""
    price_per_night: float = 0
    accepted_payments: List[str] = strawberry.field(default_factory=list)
    payout_method: str = ""
    payout_account: str = ""
    # Images as base64 data-URLs ("data:image/...;base64,...") from the client.
    images: List[str] = strawberry.field(default_factory=list)


@strawberry.type
class BookingType:
    """A guest's reservation, as shown on the 'My Bookings' page."""

    id: strawberry.ID
    villa_id: strawberry.ID
    villa_title: str
    villa_cover: str
    villa_city: str
    villa_country: str
    guest_name: str
    guest_avatar: str
    guest_email: str
    check_in: str
    check_out: str
    nights: int
    guests: int
    price_per_night: float
    subtotal: float
    discount: float
    coupon_code: str
    service_fee: float
    tax: float
    # Extra services the guest chose (name + per-night price) and their summed
    # cost (price × nights). Frozen at booking time, added straight into total.
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
    # Derived lifecycle: upcoming / awaiting_checkin / staying / completed /
    # no_show / cancelled — computed live from the clock (see Booking).
    lifecycle_status: str
    # Hours past the scheduled check-in with the guest still not checked in
    # (0 unless awaiting_checkin) — drives the "X hrs late" text.
    hours_late: float
    # Whether the guest may still cancel, and the fine a cancellation right now
    # would carry (0 when free). For cancelled bookings, `cancellation_fee` is
    # what was actually charged and `refund_amount` what's returned.
    can_cancel: bool
    cancel_fee_now: float
    cancellation_fee: float
    refund_amount: float
    # Reviews: whether the guest may review this stay (completed + not yet
    # reviewed), and their review if they've already left one (0 / "" if not).
    can_review: bool
    review_rating: int
    review_comment: str

    @classmethod
    def from_model(cls, booking, request=None) -> "BookingType":
        villa = booking.villa
        guest = booking.guest
        owner = villa.owner
        # One clock reading drives every derived value below, so the lifecycle,
        # "hours late" and the live cancellation fee can't disagree with each
        # other by the odd millisecond.
        now = timezone.now()
        cancelled = booking.status == booking.STATUS_CANCELLED
        stored_fee = float(booking.cancellation_fee or 0)
        # The guest's review of this stay, if any (reverse one-to-one).
        try:
            review = booking.review
        except ObjectDoesNotExist:
            review = None
        # A stay can be reviewed once it's completed (checked out), isn't
        # cancelled, and hasn't been reviewed yet.
        can_review = (
            booking.checked_out_at is not None and not cancelled and review is None
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
            guest_name=(guest.full_name or guest.email or "").strip(),
            guest_avatar=guest.avatar or "",
            guest_email=guest.email or "",
            check_in=booking.check_in.isoformat(),
            check_out=booking.check_out.isoformat(),
            nights=booking.nights,
            guests=booking.guests,
            price_per_night=float(booking.price_per_night),
            subtotal=float(booking.subtotal),
            discount=float(booking.discount),
            coupon_code=booking.coupon_code or "",
            service_fee=float(booking.service_fee),
            tax=float(booking.tax),
            extra_services=[
                ExtraServiceType(name=s.get("name", ""), price=float(s.get("price", 0) or 0))
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
            check_in_at=booking.check_in_datetime().replace(tzinfo=None).isoformat(),
            check_out_at=booking.check_out_datetime().replace(tzinfo=None).isoformat(),
            lifecycle_status=booking.lifecycle_status(now),
            hours_late=booking.hours_late(now),
            can_cancel=booking.can_cancel(now),
            cancel_fee_now=float(booking.cancel_fee_at(now)),
            cancellation_fee=stored_fee,
            refund_amount=(float(booking.total) - stored_fee) if cancelled else 0.0,
            can_review=can_review,
            review_rating=int(review.rating) if review else 0,
            review_comment=(review.comment or "") if review else "",
        )


@strawberry.input
class BookingInput:
    villa_id: strawberry.ID
    check_in: str  # ISO date "YYYY-MM-DD"
    check_out: str  # ISO date "YYYY-MM-DD"
    guests: int = 1
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
