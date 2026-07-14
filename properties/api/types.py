from typing import List

import strawberry


@strawberry.type
class VillaImageType:
    """One stored villa photo — id + resolvable URL (used when editing)."""

    id: strawberry.ID
    url: str


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
    bathrooms: int
    guests: int
    services: List[str]
    price_per_night: float
    accepted_payments: List[str]
    payout_method: str
    payout_account: str
    images: List[str]
    photos: List[VillaImageType]
    cover_image: str
    created_at: str

    @classmethod
    def from_model(cls, villa, request=None) -> "VillaType":
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
            bathrooms=villa.bathrooms,
            guests=villa.guests,
            services=list(villa.services or []),
            price_per_night=float(villa.price_per_night),
            accepted_payments=list(villa.accepted_payments or []),
            payout_method=villa.payout_method,
            payout_account=villa.payout_account,
            images=image_urls,
            photos=photos,
            cover_image=image_urls[0] if image_urls else "",
            created_at=villa.created_at.isoformat(),
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
    bathrooms: int = 1
    guests: int = 1
    services: List[str] = strawberry.field(default_factory=list)
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
