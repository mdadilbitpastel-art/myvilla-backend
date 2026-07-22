from typing import List

import strawberry

from datetime import date as _date

from properties import availability


def _today():
    return _date.today()


@strawberry.type
class VillaImageType:
    """One stored villa photo — id + resolvable URL (used when editing)."""

    id: strawberry.ID
    url: str


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
class VillaType:
    id: strawberry.ID
    owner_id: strawberry.ID
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
        return cls(
            id=strawberry.ID(str(villa.id)),
            owner_id=strawberry.ID(str(villa.owner_id)),
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
            check_in_time=_hhmm(villa.check_in_time),
            check_out_time=_hhmm(villa.check_out_time),
            pets_allowed=villa.pets_allowed,
            smoking_allowed=villa.smoking_allowed,
            events_allowed=villa.events_allowed,
            additional_rules=villa.additional_rules or "",
            house_rules=_house_rules(villa),
            price_per_night=float(villa.price_per_night),
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
    service_fee: float
    tax: float
    total: float
    payment_method: str
    card_last4: str
    status: str
    host_responded: bool
    created_at: str

    @classmethod
    def from_model(cls, booking, request=None) -> "BookingType":
        villa = booking.villa
        guest = booking.guest

        def absolute(url: str) -> str:
            # Only local media paths ("/media/...") need the host prefix;
            # full URLs and data-URLs (avatars) pass through untouched.
            if request is not None and url and url.startswith("/"):
                return request.build_absolute_uri(url)
            return url

        return cls(
            id=strawberry.ID(str(booking.id)),
            villa_id=strawberry.ID(str(villa.id)),
            villa_title=villa.title,
            villa_cover=absolute(villa.cover_image_url),
            villa_city=villa.city,
            villa_country=villa.country,
            guest_name=(guest.full_name or guest.email or "").strip(),
            guest_avatar=guest.avatar or "",
            guest_email=guest.email or "",
            check_in=booking.check_in.isoformat(),
            check_out=booking.check_out.isoformat(),
            nights=booking.nights,
            guests=booking.guests,
            price_per_night=float(booking.price_per_night),
            subtotal=float(booking.subtotal),
            service_fee=float(booking.service_fee),
            tax=float(booking.tax),
            total=float(booking.total),
            payment_method=booking.payment_method,
            card_last4=booking.card_last4,
            status=booking.status,
            host_responded=booking.host_responded,
            created_at=booking.created_at.isoformat(),
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
    billing_street: str = ""
    billing_apartment: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_zip: str = ""
    billing_country: str = ""
    contact_email: str = ""
    contact_phone: str = ""
