from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

import strawberry
from django.db.models import Avg, Count, Q
from django.utils import timezone
from graphql import GraphQLError

from accounts.auth import get_authenticated_user
from accounts.security import require_authenticated_user
from properties import availability, coupons as coupon_utils
from properties.models import Booking, Coupon, Favorite, Review, Villa, VillaBlockedDate
from .types import (
    BookedRangeType,
    BookingType,
    BookingWindowType,
    CouponPreviewType,
    CouponType,
    OfferType,
    ReviewType,
    VillaAvailabilityType,
    VillaType,
    WelcomeOfferType,
)

# Fields a free-text search looks at: the villa's own name plus every part of
# its location, so "villa name, city or country" all work from the one box.
_SEARCH_FIELDS = ("title", "city", "country", "address", "property_type")


def _search_filter(search):
    """
    Build the Q for the search box, or None when there's nothing to search for.

    Each whitespace-separated word is matched as a *substring* (icontains), so
    a partial word finds the villa — "gond" matches "Gondava", "bal" matches
    "Bali". A word may land in any of the searched fields, but every word has
    to match somewhere: that's what makes a two-part query like "casa bali"
    (name + country) narrow the results instead of widening them.
    """
    words = (search or "").split()
    if not words:
        return None

    condition = None
    for word in words:
        matches_any_field = Q()
        for field in _SEARCH_FIELDS:
            matches_any_field |= Q(**{f"{field}__icontains": word})
        condition = matches_any_field if condition is None else condition & matches_any_field
    return condition


def _with_availability(
    villas, request, check_in=None, check_out=None, guests=None, viewer=None
):
    """
    Turn villas into VillaTypes that know whether they can take the stay.

    One query answers it for the whole page rather than one per villa, and the
    result is the same object shape everywhere — search results, the detail
    page and the host's own property list all read the same two fields.
    """
    villas = list(villas)

    # Availability is only judged when the guest actually asked about it — with
    # dates or a party size. With NO such filter there is nothing to be
    # "unavailable" for, so every villa is shown plainly (no badge, no reason):
    # a bare listing page must never accuse a villa of being booked for a stay
    # nobody searched for.
    has_dates = check_in is not None
    has_guests = bool(guests)
    if not has_dates and not has_guests:
        return [
            VillaType.from_model(
                v,
                request=request,
                is_available=True,
                unavailable_reason="",
                viewer=viewer,
            )
            for v in villas
        ]

    ids = [v.id for v in villas]
    # Only look at the calendar when dates were given; a guests-only search must
    # not pull in date reasons (and vice-versa).
    if has_dates:
        start, end = availability.normalise_range(check_in, check_out)
        free_from = availability.booked_until(ids, start, end)
        blocked = availability.blocked_nights(ids, start, end)
    else:
        end = None
        free_from = {}
        blocked = {}

    out = []
    for v in villas:
        reasons = availability.unavailable_reasons(
            v,
            guests=guests if has_guests else None,
            free_from=free_from.get(v.id) if has_dates else None,
            # Only when the guest named a check-in date: this is what catches a
            # date that has slipped behind the villa's check-in time.
            check_in=check_in if has_dates else None,
            check_out=end if has_dates else None,
            blocked_on=blocked.get(v.id) if has_dates else None,
        )
        out.append(
            VillaType.from_model(
                v,
                request=request,
                is_available=not reasons,
                # All applicable reasons together, so a stay that's both too big
                # a party AND booked shows both, not just the first.
                unavailable_reason=" · ".join(reasons),
                viewer=viewer,
            )
        )
    return out


def build_villa_availability(villa, days: int = 120) -> VillaAvailabilityType:
    """
    Build one villa's owner calendar. Shared by the query and by the two
    mutations that change it, so a host always gets the same object back
    and the panel never has to guess what the change did.
    """
    days = max(7, min(days, 365))
    start = date.today()
    end = start + timedelta(days=days)

    bookings = (
        Booking.objects.filter(
            villa=villa,
            status=Booking.STATUS_ACTIVE,
            check_out__gt=start,
            check_in__lt=end,
        )
        .select_related("guest")
        .order_by("check_in")
    )

    booked_dates = set()
    upcoming = []
    max_guests = 0
    for b in bookings:
        # Half-open: the check-out day is free for the next guest, so it is
        # NOT one of the occupied nights.
        night = max(b.check_in, start)
        while night < min(b.check_out, end):
            booked_dates.add(night)
            night += timedelta(days=1)
        max_guests = max(max_guests, b.guests)
        upcoming.append(
            BookedRangeType(
                booking_id=strawberry.ID(str(b.id)),
                check_in=b.check_in.isoformat(),
                check_out=b.check_out.isoformat(),
                nights=b.nights,
                guests=b.guests,
                guest_name=(b.guest.full_name or b.guest.email or "Guest"),
            )
        )

    # Dates the host closed by hand. Held for the whole window, including the
    # part beyond `availability_days` — closing a date months out is exactly
    # the case this exists for, and it must still be visible on the calendar.
    blocked = sorted(
        d
        for d in VillaBlockedDate.objects.filter(
            villa=villa, date__gte=start, date__lt=end
        ).values_list("date", flat=True)
    )

    free_from = availability.booked_until(
        [villa.id], start, start + timedelta(days=1)
    ).get(villa.id)
    # Tonight counts as unavailable if it's closed by hand, not only if booked.
    closed_tonight = start in set(blocked)

    return VillaAvailabilityType(
        blocked_dates=[d.isoformat() for d in blocked],
        availability_days=villa.availability_days,
        bookable_until=availability.window_end(villa).isoformat(),
        villa_id=strawberry.ID(str(villa.id)),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        is_available_now=free_from is None and not closed_tonight,
        free_from=free_from.isoformat() if free_from else "",
        booked_dates=[d.isoformat() for d in sorted(booked_dates)],
        upcoming=upcoming,
        max_booked_guests=max_guests,
    )


@strawberry.type
class PropertyQuery:
    @strawberry.field
    def my_bookings(self, info: strawberry.Info) -> List[BookingType]:
        """Bookings made by the current user, newest first. Requires a session."""
        user = require_authenticated_user(info)
        bookings = (
            Booking.objects.filter(guest=user)
            .select_related("villa", "guest", "villa__owner", "review")
            .prefetch_related("villa__images")
            .order_by("-created_at")
        )
        request = info.context.request
        return [BookingType.from_model(b, request=request) for b in bookings]

    @strawberry.field
    def pending_review_booking(
        self, info: strawberry.Info
    ) -> Optional[BookingType]:
        """
        The current guest's oldest completed-but-unreviewed stay, or null. Drives
        the "rate your stay" popup shown on the landing page. Public-safe: returns
        null when signed out rather than erroring.
        """
        user = get_authenticated_user(info)
        if user is None:
            return None
        booking = (
            Booking.objects.filter(
                guest=user, checked_out_at__isnull=False, review__isnull=True
            )
            .exclude(status=Booking.STATUS_CANCELLED)
            .select_related("villa", "guest", "villa__owner", "review")
            .prefetch_related("villa__images")
            .order_by("checked_out_at")
            .first()
        )
        if booking is None:
            return None
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.field
    def villa_reviews(
        self, info: strawberry.Info, villa_id: strawberry.ID, limit: int = 50
    ) -> List[ReviewType]:
        """Public: the reviews left on one villa, newest first."""
        viewer = get_authenticated_user(info)
        limit = max(1, min(limit, 100))
        reviews = (
            Review.objects.filter(villa_id=villa_id)
            .select_related("guest", "villa")
            .order_by("-created_at")[:limit]
        )
        return [ReviewType.from_model(r, viewer=viewer) for r in reviews]

    @strawberry.field
    def latest_reviews(
        self, info: strawberry.Info, limit: int = 24
    ) -> List[ReviewType]:
        """
        Public: the most recent reviews across ALL villas that carry written
        text — for the landing-page testimonials. Empty-comment ratings are left
        out (a testimonial card needs something to say).
        """
        viewer = get_authenticated_user(info)
        limit = max(1, min(limit, 60))
        reviews = (
            Review.objects.exclude(comment="")
            .select_related("guest", "villa")
            .order_by("-created_at")[:limit]
        )
        return [ReviewType.from_model(r, viewer=viewer) for r in reviews]

    @strawberry.field
    def my_villa_bookings(self, info: strawberry.Info) -> List[BookingType]:
        """
        Bookings made on villas the current user OWNS — i.e. the host's
        incoming rent requests. Newest first. Requires a session.
        """
        user = require_authenticated_user(info)
        bookings = (
            Booking.objects.filter(villa__owner=user)
            .select_related("villa", "guest", "villa__owner", "review")
            .prefetch_related("villa__images")
            .order_by("-created_at")
        )
        request = info.context.request
        return [BookingType.from_model(b, request=request) for b in bookings]

    @strawberry.field
    def my_favorites(self, info: strawberry.Info) -> List[VillaType]:
        """Villas the current user has saved to their wishlist. Requires a session."""
        user = require_authenticated_user(info)
        villas = (
            Villa.objects.filter(favorited_by__user=user)
            .select_related("owner").annotate(avg_rating=Avg("reviews__rating"), review_count=Count("reviews", distinct=True)).prefetch_related("images")
            .order_by("-favorited_by__created_at")
        )
        return _with_availability(villas, info.context.request, viewer=user)

    @strawberry.field
    def my_villas_count(self, info: strawberry.Info) -> int:
        """
        How many villas the current user owns. Cheap enough to call from the
        account sidebar on every page: the host-only sections (Rent Requests,
        Coupons) appear only once this is at least 1, and hide again the moment
        the last property is removed.
        """
        user = require_authenticated_user(info)
        return Villa.objects.filter(owner=user).count()

    @strawberry.field
    def my_coupons(self, info: strawberry.Info) -> List[CouponType]:
        """Discount codes the current user has created. Newest first."""
        user = require_authenticated_user(info)
        coupons = (
            Coupon.objects.filter(owner=user)
            .select_related("villa")
            .order_by("-created_at")
        )
        return [CouponType.from_model(c) for c in coupons]

    @strawberry.field
    def public_offers(self, info: strawberry.Info, limit: int = 8) -> List[OfferType]:
        """
        Public: live offers for the landing page — real villas paired with an
        active coupon that applies to them. A villa-scoped coupon shows that
        villa; a common coupon shows the owner's newest villa as a stand-in.
        At most one offer per villa, newest coupon first.
        """
        limit = max(1, min(limit, 24))
        request = info.context.request
        offers: List[OfferType] = []
        seen_villas = set()

        # Live today: switched on AND inside its validity period. A code the
        # home page advertises has to be one the guest can actually redeem —
        # a scheduled or expired coupon must never reach an offer card.
        today = timezone.localdate()
        coupons = (
            Coupon.objects.filter(active=True)
            .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today))
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
            .select_related("villa")
            .order_by("-created_at")
        )
        # Cache each owner's representative (newest) villa for their common coupons.
        rep_cache: dict = {}
        for coupon in coupons:
            if coupon.villa_id is not None:
                villa = coupon.villa
            else:
                if coupon.owner_id not in rep_cache:
                    rep_cache[coupon.owner_id] = (
                        Villa.objects.filter(owner_id=coupon.owner_id)
                        .select_related("owner").annotate(avg_rating=Avg("reviews__rating"), review_count=Count("reviews", distinct=True)).prefetch_related("images")
                        .order_by("-created_at")
                        .first()
                    )
                villa = rep_cache[coupon.owner_id]
            if villa is None or villa.id in seen_villas:
                continue
            seen_villas.add(villa.id)
            offers.append(OfferType.from_pair(villa, coupon, request=request))
            if len(offers) >= limit:
                break
        return offers

    @strawberry.field
    def validate_coupon(
        self,
        info: strawberry.Info,
        code: str,
        villa_id: strawberry.ID,
        nights: int = 1,
    ) -> CouponPreviewType:
        """
        Public: the payment page's live preview of a code against a villa and a
        night count. Returns whether it applies and, if so, the amount off this
        stay's accommodation subtotal (the same figure the booking will freeze).
        """
        villa = Villa.objects.filter(pk=villa_id).first()
        if villa is None:
            return CouponPreviewType(
                valid=False,
                message="Villa not found.",
                code=coupon_utils.normalise_code(code),
                discount_type="",
                discount_value=0,
                discount=0,
                label="",
            )
        try:
            coupon = coupon_utils.resolve_coupon(code, villa)
        except coupon_utils.CouponError as exc:
            return CouponPreviewType(
                valid=False,
                message=str(exc),
                code=coupon_utils.normalise_code(code),
                discount_type="",
                discount_value=0,
                discount=0,
                label="",
            )
        if coupon is None:
            return CouponPreviewType(
                valid=False,
                message="Enter a coupon code.",
                code="",
                discount_type="",
                discount_value=0,
                discount=0,
                label="",
            )
        subtotal = Decimal(str(villa.price_per_night)) * max(1, nights)
        amount = coupon_utils.discount_for(coupon, subtotal)
        return CouponPreviewType(
            valid=True,
            message=f"{coupon_utils.label_for(coupon)} applied.",
            code=coupon.code,
            discount_type=coupon.discount_type,
            discount_value=float(coupon.discount_value),
            discount=float(amount),
            label=coupon_utils.label_for(coupon),
        )

    @strawberry.field
    def my_villas(self, info: strawberry.Info) -> List[VillaType]:
        """
        Villas owned by the current user. Requires a valid session. Carries the
        same availability as the public pages, so a host sees on their own
        property list exactly what a guest browsing today sees.
        """
        user = require_authenticated_user(info)
        villas = (
            Villa.objects.filter(owner=user)
            .select_related("owner").annotate(avg_rating=Avg("reviews__rating"), review_count=Count("reviews", distinct=True)).prefetch_related("images")
            .order_by("-created_at")
        )
        return _with_availability(villas, info.context.request, viewer=user)

    @strawberry.field
    def villa_availability(
        self,
        info: strawberry.Info,
        villa_id: strawberry.ID,
        days: int = 120,
    ) -> VillaAvailabilityType:
        """
        The owner's view of one villa's calendar: which nights are already
        taken, by whom, and when it next frees up.

        Owner-only — it names guests. A host editing their listing needs this
        in front of them: changing rooms or capacity while a stay is booked is
        exactly when they need to know a stay IS booked.
        """
        user = require_authenticated_user(info)
        villa = Villa.objects.filter(pk=villa_id, owner=user).first()
        if villa is None:
            raise GraphQLError("Villa not found.")
        return build_villa_availability(villa, days)

    @strawberry.field
    def welcome_offer(self, info: strawberry.Info) -> WelcomeOfferType:
        """
        Public: the first-booking welcome offer for whoever is asking.

        Answers for signed-out visitors too — they haven't booked either, and
        the offer exists to bring them in. `createBooking` re-checks eligibility
        under a lock before it applies anything, so this is only ever what to
        show, never what to charge.
        """
        return WelcomeOfferType.for_viewer(get_authenticated_user(info))

    @strawberry.field
    def booking_window(
        self, info: strawberry.Info, villa_id: strawberry.ID
    ) -> BookingWindowType:
        """
        Public: the dates a guest may pick for this villa — the host's rolling
        window plus the dates inside it that are already taken or closed.

        Public on purpose, and deliberately thinner than `villaAvailability`:
        this names no guests and reveals nothing beyond "you can't have that
        night", which is exactly what the reservation calendar has to know.
        """
        villa = Villa.objects.filter(pk=villa_id).first()
        if villa is None:
            raise GraphQLError("Villa not found.")
        return BookingWindowType.from_model(villa)

    @strawberry.field
    def villas(
        self,
        info: strawberry.Info,
        limit: int = 24,
        search: Optional[str] = None,
        category: Optional[str] = None,
        guests: Optional[int] = None,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
    ) -> List[VillaType]:
        """
        Public: listed villas. Used by both the landing page (no filters) and
        the search page (any combination of filters).
        - `search`   villa name / location — see `_search_filter` below
        - `category` matches the property type exactly ("All" = no filter)
        - `guests`   party size, and `check_in`/`check_out` the nights wanted

        Note what `guests` and the dates do NOT do: they don't drop villas from
        the results. A guest who searched a villa by name should still find it,
        told plainly that it's booked or too small, rather than being shown an
        empty page. Only what the guest asked to see — the text, the category,
        the price — removes a villa. Unavailable ones sort to the end.
        - `min_price` / `max_price` price-per-night range
        """
        limit = max(1, min(limit, 60))
        qs = Villa.objects.select_related("owner").annotate(avg_rating=Avg("reviews__rating"), review_count=Count("reviews", distinct=True)).prefetch_related("images")

        condition = _search_filter(search)
        if condition is not None:
            qs = qs.filter(condition)

        cat = (category or "").strip()
        if cat and cat.lower() != "all":
            qs = qs.filter(property_type__iexact=cat)

        if min_price is not None:
            qs = qs.filter(price_per_night__gte=min_price)
        if max_price is not None:
            qs = qs.filter(price_per_night__lte=max_price)

        qs = qs.order_by("-created_at")[:limit]
        results = _with_availability(
            qs,
            info.context.request,
            viewer=get_authenticated_user(info),
            check_in=availability.parse_date(check_in),
            check_out=availability.parse_date(check_out),
            guests=guests,
        )
        # Stable: available first, each group still newest-first from the query.
        results.sort(key=lambda v: not v.is_available)
        return results

    @strawberry.field
    def villa(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        guests: Optional[int] = None,
    ) -> Optional[VillaType]:
        """
        Public: a single villa by id (used by the detail page). Pass the dates
        and party size to have its availability answered for that exact stay.
        """
        v = Villa.objects.select_related("owner").annotate(avg_rating=Avg("reviews__rating"), review_count=Count("reviews", distinct=True)).prefetch_related("images").filter(pk=id).first()
        if v is None:
            return None
        return _with_availability(
            [v],
            info.context.request,
            viewer=get_authenticated_user(info),
            check_in=availability.parse_date(check_in),
            check_out=availability.parse_date(check_out),
            guests=guests,
        )[0]
