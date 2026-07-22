from datetime import date, timedelta
from typing import List, Optional

import strawberry
from django.db.models import Q
from graphql import GraphQLError

from accounts.auth import get_authenticated_user
from accounts.security import require_authenticated_user
from properties import availability
from properties.models import Booking, Favorite, Villa, VillaBlockedDate
from .types import BookedRangeType, BookingType, VillaAvailabilityType, VillaType

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
    start, end = availability.normalise_range(check_in, check_out)
    ids = [v.id for v in villas]
    free_from = availability.booked_until(ids, start, end)
    blocked = availability.blocked_nights(ids, start, end)
    out = []
    for v in villas:
        reason = availability.unavailable_reason(
            v,
            free_from.get(v.id),
            guests,
            check_out=end if check_in else None,
            blocked_on=blocked.get(v.id),
        )
        out.append(
            VillaType.from_model(
                v,
                request=request,
                is_available=not reason,
                unavailable_reason=reason,
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
            .select_related("villa", "guest")
            .prefetch_related("villa__images")
            .order_by("-created_at")
        )
        request = info.context.request
        return [BookingType.from_model(b, request=request) for b in bookings]

    @strawberry.field
    def my_villa_bookings(self, info: strawberry.Info) -> List[BookingType]:
        """
        Bookings made on villas the current user OWNS — i.e. the host's
        incoming rent requests. Newest first. Requires a session.
        """
        user = require_authenticated_user(info)
        bookings = (
            Booking.objects.filter(villa__owner=user)
            .select_related("villa", "guest")
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
            .prefetch_related("images")
            .order_by("-favorited_by__created_at")
        )
        return _with_availability(villas, info.context.request, viewer=user)

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
            .prefetch_related("images")
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
        qs = Villa.objects.prefetch_related("images")

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
        v = Villa.objects.prefetch_related("images").filter(pk=id).first()
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
