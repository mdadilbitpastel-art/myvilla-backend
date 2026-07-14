from typing import List, Optional

import strawberry
from django.db.models import Q

from accounts.security import require_authenticated_user
from properties.models import Booking, Villa
from .types import BookingType, VillaType


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
    def my_villas(self, info: strawberry.Info) -> List[VillaType]:
        """Villas owned by the current user. Requires a valid session."""
        user = require_authenticated_user(info)
        villas = (
            Villa.objects.filter(owner=user)
            .prefetch_related("images")
            .order_by("-created_at")
        )
        request = info.context.request
        return [VillaType.from_model(v, request=request) for v in villas]

    @strawberry.field
    def villas(
        self,
        info: strawberry.Info,
        limit: int = 24,
        search: Optional[str] = None,
        category: Optional[str] = None,
        guests: Optional[int] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
    ) -> List[VillaType]:
        """
        Public: listed villas, newest first. Used by both the landing page
        (no filters) and the search page (any combination of filters).
        - `search`   matches title / city / country / address / property type
        - `category` matches the property type exactly ("All" = no filter)
        - `guests`   minimum guest capacity
        - `min_price` / `max_price` price-per-night range
        """
        limit = max(1, min(limit, 60))
        qs = Villa.objects.prefetch_related("images")

        term = (search or "").strip()
        if term:
            qs = qs.filter(
                Q(title__icontains=term)
                | Q(city__icontains=term)
                | Q(country__icontains=term)
                | Q(address__icontains=term)
                | Q(property_type__icontains=term)
            )

        cat = (category or "").strip()
        if cat and cat.lower() != "all":
            qs = qs.filter(property_type__iexact=cat)

        if guests:
            qs = qs.filter(guests__gte=guests)
        if min_price is not None:
            qs = qs.filter(price_per_night__gte=min_price)
        if max_price is not None:
            qs = qs.filter(price_per_night__lte=max_price)

        qs = qs.order_by("-created_at")[:limit]
        request = info.context.request
        return [VillaType.from_model(v, request=request) for v in qs]

    @strawberry.field
    def villa(self, info: strawberry.Info, id: strawberry.ID) -> Optional[VillaType]:
        """Public: a single villa by id (used by the detail page)."""
        v = Villa.objects.prefetch_related("images").filter(pk=id).first()
        if v is None:
            return None
        return VillaType.from_model(v, request=info.context.request)
