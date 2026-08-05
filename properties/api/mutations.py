import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

import strawberry
from django.db import transaction
from django.utils import timezone
from graphql import GraphQLError

from django.contrib.auth import get_user_model

from accounts.security import require_authenticated_user
from properties import (
    additions as addons,
    availability,
    checkin,
    coupons as coupon_utils,
    welcome,
    whatsapp,
)
from properties.images import data_url_to_file
from properties.models import (
    DEFAULT_GRACE_MINUTES,
    Booking,
    Coupon,
    Favorite,
    Review,
    Villa,
    VillaBlockedDate,
    VillaImage,
)
from .types import (
    AddonPaymentInput,
    BookingInput,
    BookingType,
    CouponInput,
    CouponType,
    VillaInput,
    VillaType,
    _clean_extra_services,
)

# How many photos one villa may carry. The wizard stops a host at the same
# number; this is the rule itself, for anything that reaches the API another way.
MAX_IMAGES = 10

# Platform service fee applied on top of the accommodation subtotal, and the
# flat tax on it. Defined in properties/additions.py and imported here so a
# night added to a stay later is charged exactly what a night booked at checkout
# was. Mirrored by TAX_RATE in the frontend's lib/pricing.ts.
SERVICE_FEE_RATE = addons.SERVICE_FEE_RATE
TAX_RATE = addons.TAX_RATE

# How far ahead a host may open their calendar (see Villa.availability_days).
MAX_AVAILABILITY_DAYS = 365

# The ceiling on a single stay is the host's own window, not a number of ours:
# a villa opened for two months can be booked for two months. This constant is
# only the outer bound that window can ever reach, kept so an absurd request is
# still refused with a sentence rather than by arithmetic.
MAX_BOOKING_NIGHTS = MAX_AVAILABILITY_DAYS


def _money(value) -> Decimal:
    """Round any numeric to 2 decimal places, half-up (currency)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _segments_json(segments) -> list:
    """A split stay's (check_in, check_out) runs, as the JSON the column holds."""
    return [
        {"check_in": start.isoformat(), "check_out": end.isoformat()}
        for start, end in segments
    ]


def _mask_account(raw: str) -> str:
    """Store payout accounts safely: keep only the last 4 digits, masked."""
    account = (raw or "").strip()
    if not account:
        return ""
    digits = _digits(account)
    if len(digits) >= 4:
        return "•••• " + digits[-4:]
    return account


# Payment methods whose guest flow is a card (number + expiry + CVV). Anything
# else (PayPal, Google Pay) is an account-based flow with no card fields.
CARD_PAYMENT_METHODS = {"Visa", "Mastercard", "Credit Card", "Debit Card"}


def _mask_reference(raw: str) -> str:
    """Mask a PayPal / Google Pay account for storage.

    E-mails and UPI ids ("name@bank") keep their first character and domain
    ("j••••@gmail.com"); anything else is masked to its last 4 digits. Capped to
    the Booking.card_last4 column width.
    """
    ref = (raw or "").strip()
    if not ref:
        return ""
    if "@" in ref:
        local, _, domain = ref.partition("@")
        head = local[:1] or "•"
        return f"{head}{'•' * 4}@{domain}"[:24]
    return _mask_account(ref)[:24]


def _accepted_method(villa, raw: str) -> str:
    """The chosen payment method, checked against what this host takes.

    Defence in depth: the UI only offers accepted methods, but the client can't
    be trusted, and a villa that takes only PayPal must not receive a card.
    """
    method = (raw or "").strip()
    if not method:
        raise GraphQLError("Please choose a payment method.")
    accepted = [str(p).strip() for p in (villa.accepted_payments or []) if str(p).strip()]
    if accepted and method not in accepted:
        raise GraphQLError("This villa does not accept that payment method.")
    return method


def _saved_payment(user, villa, raw_method):
    """
    The guest's own last payment with this method — masked reference and the
    billing address that went with it — or a refusal.

    Looked up from THEIR bookings rather than taken from the page: the client
    sends a method name and nothing else, so a tampered request can only ever
    reach a card this same guest has genuinely used. Nothing sensitive is
    involved either way, since only the masked tail was ever stored.
    """
    method = _accepted_method(villa, raw_method)
    row = (
        Booking.objects.filter(guest=user, payment_method=method)
        .exclude(card_last4="")
        .order_by("-created_at")
        .first()
    )
    if row is None:
        raise GraphQLError(
            f"You have no saved {method} details. Please enter them below."
        )
    return method, row


def _resolve_payment(villa, data):
    """
    Validate the payment on `data` against what this host accepts, and return
    `(method, masked_reference)`.

    The guest pays with ONE of the methods the host offers. Card brands
    (Visa/Mastercard) need card fields plus a billing address; PayPal and Google
    Pay need only the account reference — so exactly what the chosen method
    requires is checked, and a PayPal payment is never refused for a missing
    card. Nothing sensitive survives the call: a card is reduced to its last
    four digits and an account handle is masked.

    Shared by checkout and by every purchase made against a booking afterwards
    (see properties/additions.py), so a stay extended next week is paid for
    under the same rules as the stay itself.
    """
    method = _accepted_method(villa, data.payment_method)

    if method in CARD_PAYMENT_METHODS:
        if len(_digits(data.card_number)) < 12:
            raise GraphQLError("Enter a valid card number.")
        if not (data.expiration or "").strip():
            raise GraphQLError("Enter the card expiration date.")
        cvv = _digits(data.cvv)
        if len(cvv) < 3 or len(cvv) > 4:
            raise GraphQLError("Enter a valid CVV.")

        # --- Billing address (mandatory for card payments) ---
        if not (data.billing_street or "").strip():
            raise GraphQLError("Enter your billing street name.")
        if not (data.billing_city or "").strip():
            raise GraphQLError("Enter your billing city.")
        if not (data.billing_country or "").strip():
            raise GraphQLError("Select your billing country or region.")
        return method, _mask_account(data.card_number)

    # Account-based methods (PayPal / Google Pay): the reference must be an
    # e-mail / UPI-style handle.
    reference = (data.payment_detail or "").strip()
    if "@" not in reference:
        if method == "PayPal":
            raise GraphQLError("Enter the e-mail for your PayPal account.")
        raise GraphQLError("Enter your UPI ID (name@bank) or Google account e-mail.")
    return method, _mask_reference(reference)


def _addon_payment(booking, payment: AddonPaymentInput):
    """
    Settle how an addition to `booking` is being paid for.

    Either the card already on the booking — the guest has paid with it once and
    should not have to type it again — or a fresh method, validated exactly as
    checkout validates one. The saved route is refused rather than silently
    falling through when the booking has nothing on file, so nobody is ever
    charged against a payment method that isn't there.
    """
    if payment is not None and payment.use_saved:
        if not booking.payment_method:
            raise GraphQLError(
                "This booking has no saved payment method. Please choose one."
            )
        return booking.payment_method, booking.card_last4
    return _resolve_payment(booking.villa, payment)


def _parse_time(value: str, label: str):
    """
    "HH:MM" (what <input type="time"> submits) -> a `time`, or None when the
    host left it blank. Anything else is rejected rather than silently dropped,
    so a broken client can't quietly wipe a rule the guest relies on.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:5], "%H:%M").time()
    except ValueError:
        raise GraphQLError(f"Enter a valid {label} time.")


def _own_booking(info, id) -> Booking:
    """
    The booking the CALLER MADE, or a refusal — the guest's side of the pair
    below, shared by the two cancellation paths.

    Deliberately does not refuse an already-cancelled booking: the cancellation
    policy has its own wording for that ("This booking is already cancelled."),
    and the guest should read the same sentence here as on the booking itself.
    """
    user = require_authenticated_user(info)
    booking = (
        Booking.objects.select_related("villa", "guest", "villa__owner", "review")
        .prefetch_related("cancellations", "additions")
        .filter(pk=id, guest=user)
        .first()
    )
    if booking is None:
        raise GraphQLError("Booking not found.")
    return booking


def _owned_booking(info, id) -> Booking:
    """
    The caller's own booking, or a refusal. Shared by the check-in steps.

    Module-level, not a method: inside a Strawberry resolver `self` is the root
    value — which for a root mutation is None — so `self._helper(...)` fails
    with "'NoneType' object has no attribute ...". Resolvers only ever share
    code through plain functions like this one.
    """
    user = require_authenticated_user(info)
    booking = (
        Booking.objects.select_related("villa", "guest", "villa__owner", "review")
        .filter(pk=id, villa__owner=user)
        .first()
    )
    if booking is None:
        raise GraphQLError("Booking not found.")
    if booking.status == Booking.STATUS_CANCELLED:
        raise GraphQLError("This booking was cancelled.")
    return booking


def _validate_common(user, data: VillaInput):
    """
    Enforce the mandatory fields shared by create & update: personal details
    (Section 1, from the user's profile), villa details (2), pricing (5) and
    the payment method + account type (6). Card-number rules differ between
    create and update, so they're handled by each caller. Returns (title,
    accepted_payments).
    """
    # --- Section 1: Personal Details ---
    missing = [
        label
        for value, label in (
            (user.full_name, "full name"),
            (user.gender, "gender"),
            (user.email, "email"),
            (getattr(user, "date_of_birth", ""), "date of birth"),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise GraphQLError(
            "Complete your personal details first: " + ", ".join(missing) + "."
        )

    # --- Section 2: Villa Details ---
    title = (data.title or "").strip()
    if not title:
        raise GraphQLError("Villa name is required.")
    if not (data.description or "").strip():
        raise GraphQLError("A villa description is required.")
    if not (data.build_up_area or "").strip():
        raise GraphQLError("Villa dimensions are required.")
    if not (data.address or "").strip():
        raise GraphQLError("Villa address is required.")
    # The bed counts are the source of truth: the room count and the guest
    # capacity are computed from them (see `_apply_fields`), never taken from
    # the client, so a listing can't advertise capacity it has no beds for.
    if data.single_bed_rooms + data.double_bed_rooms < 1:
        raise GraphQLError(
            "Add at least one room — how many have a single bed, and how many a double."
        )
    if data.availability_days < 1 or data.availability_days > MAX_AVAILABILITY_DAYS:
        raise GraphQLError(
            f"Availability must be between 1 and {MAX_AVAILABILITY_DAYS} days."
        )
    # Guests plan travel around these, so a listing can't go up without them.
    if not (data.check_in_time or "").strip():
        raise GraphQLError("Set a check-in time.")
    if not (data.check_out_time or "").strip():
        raise GraphQLError("Set a check-out time.")

    # --- Section 5: Pricing ---
    if data.price_per_night is None or data.price_per_night <= 0:
        raise GraphQLError("Please enter a valid price per night.")

    # --- Section 6: Accepted methods + the host's (shared) bank payout ---
    accepted = [p.strip() for p in (data.accepted_payments or []) if p.strip()]
    if not accepted:
        raise GraphQLError("Select at least one payment method.")
    # Bank details live on the HOST now (one set for all their villas), so a
    # listing just requires the host to have added them — see updatePayoutDetails.
    if not (user.payout_account or "").strip():
        raise GraphQLError("Add your bank account details before listing a property.")

    return title, accepted


def _sync_blocked_dates(villa: Villa, raw_dates: List[str]) -> None:
    """
    Make the villa's closed nights match what the host left on the calendar.

    Only today onward is touched: past blocks are history, not something the
    form is entitled to rewrite. A night a guest has already booked is skipped
    rather than rejected — the host didn't put it there, the calendar shows it
    as booked, and failing the whole save over it would help nobody.
    """
    today = availability.today_local()
    wanted = set()
    for raw in raw_dates or []:
        try:
            day = date.fromisoformat((raw or "").strip()[:10])
        except ValueError:
            raise GraphQLError("Your calendar contains an invalid date.")
        if day >= today:
            wanted.add(day)

    booked = {
        d
        for d in wanted
        if availability.is_booked(villa.pk, d, d + timedelta(days=1))
    }
    wanted -= booked

    existing = set(
        villa.blocked_dates.filter(date__gte=today).values_list("date", flat=True)
    )
    villa.blocked_dates.filter(date__gte=today).exclude(date__in=wanted).delete()
    VillaBlockedDate.objects.bulk_create(
        [VillaBlockedDate(villa=villa, date=d) for d in sorted(wanted - existing)],
        ignore_conflicts=True,
    )


def _apply_fields(villa: Villa, data: VillaInput, title, accepted):
    """Copy validated input onto a (new or existing) villa instance."""
    villa.title = title
    villa.property_type = (data.property_type or "").strip()
    villa.city = (data.city or "").strip()
    villa.country = (data.country or "").strip()
    villa.address = (data.address or "").strip()
    villa.description = (data.description or "").strip()
    villa.build_up_area = (data.build_up_area or "").strip()
    villa.availability_days = max(1, min(data.availability_days, MAX_AVAILABILITY_DAYS))
    villa.single_bed_rooms = max(0, data.single_bed_rooms)
    villa.double_bed_rooms = max(0, data.double_bed_rooms)
    # Derived, not accepted from the client: one room per bed, and a single bed
    # sleeps one guest while a double sleeps two.
    villa.bedrooms = villa.single_bed_rooms + villa.double_bed_rooms
    villa.guests = (
        villa.single_bed_rooms * Villa.GUESTS_PER_SINGLE
        + villa.double_bed_rooms * Villa.GUESTS_PER_DOUBLE
    )
    villa.services = [s.strip() for s in (data.services or []) if s.strip()]
    villa.extra_services = _clean_extra_services(data.extra_services)
    villa.check_in_time = _parse_time(data.check_in_time, "check-in")
    villa.check_out_time = _parse_time(data.check_out_time, "check-out")
    # How long a late guest may still be checked in. 0 (the input's default,
    # i.e. the host didn't say) keeps the platform's standard window rather
    # than meaning "no grace at all", which would make every late arrival an
    # instant no-show for hosts who never opened that field.
    grace = int(data.grace_period_minutes or 0)
    villa.grace_period_minutes = grace if grace > 0 else DEFAULT_GRACE_MINUTES
    villa.pets_allowed = bool(data.pets_allowed)
    villa.smoking_allowed = bool(data.smoking_allowed)
    villa.events_allowed = bool(data.events_allowed)
    villa.additional_rules = (data.additional_rules or "").strip()
    villa.price_per_night = data.price_per_night
    villa.accepted_payments = accepted
    # Payout/bank details are stored on the HOST (User), shared by all their
    # villas — nothing payout-related is written on the villa anymore.


def _apply_image_order(image_order, cover_index, kept, new_created):
    """
    Set each VillaImage's display order and cover flag.

    `image_order` is a list of tokens — an existing image's id, or "new" for the
    next freshly-uploaded image (consumed from `new_created` in order) — giving
    the shown order. `cover_index` is the position IN THAT ORDER that the host
    picked as the cover (position-independent: choosing a cover never reorders).
    """
    by_id = {str(im.id): im for im in kept}
    new_iter = iter(new_created)
    order = [str(t).strip() for t in (image_order or [])]
    if order:
        ordered, used = [], set()
        for token in order:
            im = next(new_iter, None) if token == "new" else by_id.get(token)
            if im is None or im.pk in used:
                continue
            used.add(im.pk)
            ordered.append(im)
    else:
        ordered = list(kept) + list(new_created)
    if not ordered:
        return
    cover = cover_index if 0 <= cover_index < len(ordered) else 0
    for pos, im in enumerate(ordered):
        im.sort_order = pos
        im.is_cover = pos == cover
        im.save(update_fields=["sort_order", "is_cover"])


# Codes are stored upper-cased; this is what a valid one may contain.
_COUPON_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


def _coupon_date(value: str, label: str):
    """An optional "YYYY-MM-DD" from the coupon form, or None when left blank."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise GraphQLError(f"Enter a valid {label} date, or leave it empty.")


def _clean_coupon_input(user, data: CouponInput, exclude_id=None):
    """
    Validate a coupon create/update. Returns (code, villa_or_None, discount_type,
    discount_value, valid_from, valid_until). Raises GraphQLError with a clear
    message on any problem.

    The host must own at least one villa to have coupons at all; a villa-scoped
    coupon must name a villa they own; a common coupon (blank villa) covers them
    all. Codes are unique platform-wide, case-insensitively.
    """
    if not Villa.objects.filter(owner=user).exists():
        raise GraphQLError("Add a property before creating a coupon.")

    code = coupon_utils.normalise_code(data.code)
    if not _COUPON_CODE_RE.match(code):
        raise GraphQLError(
            "Use 3–32 letters or numbers for the code (dashes and underscores allowed)."
        )
    clash = Coupon.objects.filter(code=code)
    if exclude_id is not None:
        clash = clash.exclude(pk=exclude_id)
    if clash.exists():
        raise GraphQLError("That coupon code is already taken. Try another.")

    villa = None
    if (data.villa_id or "").strip():
        villa = Villa.objects.filter(pk=data.villa_id, owner=user).first()
        if villa is None:
            raise GraphQLError("Choose one of your own villas, or leave it for all.")

    discount_type = (data.discount_type or "").strip().lower()
    if discount_type not in (Coupon.TYPE_PERCENT, Coupon.TYPE_FIXED):
        raise GraphQLError("Choose a percentage or a fixed amount.")

    value = Decimal(str(data.discount_value or 0))
    if discount_type == Coupon.TYPE_PERCENT:
        if value <= 0 or value > 100:
            raise GraphQLError("A percentage discount must be between 1 and 100.")
    else:
        if value <= 0:
            raise GraphQLError("Enter a discount amount greater than zero.")

    # --- Validity period: both ends optional, both inclusive ---
    valid_from = _coupon_date(data.valid_from, "start")
    valid_until = _coupon_date(data.valid_until, "expiry")
    # Judged on the server's date — the same clock resolve_coupon will use when
    # a guest tries the code, so the host can't save something that is already
    # dead on arrival by the server's reckoning.
    today = timezone.localdate()
    if valid_until and valid_until < today:
        raise GraphQLError(
            "The expiry date has already passed. Pick today or a later date."
        )
    if valid_from and valid_until and valid_until < valid_from:
        raise GraphQLError("The expiry date must be on or after the start date.")

    return code, villa, discount_type, _money(value), valid_from, valid_until


@strawberry.type
class PropertyMutation:
    @strawberry.mutation
    def create_coupon(self, info: strawberry.Info, data: CouponInput) -> CouponType:
        """Create a discount code on the current user's villa(s)."""
        user = require_authenticated_user(info)
        code, villa, discount_type, value, valid_from, valid_until = _clean_coupon_input(
            user, data
        )
        coupon = Coupon.objects.create(
            owner=user,
            villa=villa,
            code=code,
            discount_type=discount_type,
            discount_value=value,
            active=bool(data.active),
            valid_from=valid_from,
            valid_until=valid_until,
        )
        return CouponType.from_model(coupon)

    @strawberry.mutation
    def update_coupon(
        self, info: strawberry.Info, id: strawberry.ID, data: CouponInput
    ) -> CouponType:
        """Update a coupon the current user owns."""
        user = require_authenticated_user(info)
        coupon = Coupon.objects.filter(pk=id, owner=user).first()
        if coupon is None:
            raise GraphQLError("Coupon not found.")
        code, villa, discount_type, value, valid_from, valid_until = _clean_coupon_input(
            user, data, exclude_id=coupon.pk
        )
        coupon.code = code
        coupon.villa = villa
        coupon.discount_type = discount_type
        coupon.discount_value = value
        coupon.active = bool(data.active)
        coupon.valid_from = valid_from
        coupon.valid_until = valid_until
        coupon.save()
        coupon = Coupon.objects.select_related("villa").get(pk=coupon.pk)
        return CouponType.from_model(coupon)

    @strawberry.mutation
    def delete_coupon(self, info: strawberry.Info, id: strawberry.ID) -> bool:
        """Delete a coupon the current user owns. Returns True on success."""
        user = require_authenticated_user(info)
        coupon = Coupon.objects.filter(pk=id, owner=user).first()
        if coupon is None:
            raise GraphQLError("Coupon not found.")
        coupon.delete()
        return True

    @strawberry.mutation
    def create_villa(self, info: strawberry.Info, data: VillaInput) -> VillaType:
        """
        Create a villa owned by the current user, saving each provided image
        through the configured storage backend. Requires a valid session.
        Every section's mandatory fields are enforced here.
        """
        user = require_authenticated_user(info)
        title, accepted = _validate_common(user, data)

        # --- Section 3: Images (at least one, freshly uploaded) ---
        images = data.images or []
        if not images:
            raise GraphQLError("Please add at least one image.")
        if len(images) > MAX_IMAGES:
            raise GraphQLError(f"You can add up to {MAX_IMAGES} images.")

        files = [data_url_to_file(img) for img in images]

        with transaction.atomic():
            villa = Villa(owner=user)
            _apply_fields(villa, data, title, accepted)
            villa.save()
            _sync_blocked_dates(villa, data.blocked_dates)
            new_created = [VillaImage.objects.create(villa=villa, image=f) for f in files]
            _apply_image_order(data.image_order, data.cover_index, [], new_created)

        return VillaType.from_model(villa, request=info.context.request, viewer=user)

    @strawberry.mutation
    def update_villa(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        data: VillaInput,
        keep_image_ids: List[strawberry.ID] = strawberry.field(default_factory=list),
    ) -> VillaType:
        """
        Update a villa the current user owns. `keep_image_ids` are the existing
        photos to keep; any not listed are removed, and `data.images` (base64)
        are added as new photos. The final photo set must have at least one.
        Leave the card number blank to keep the existing (masked) one.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=id, owner=user).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        title, accepted = _validate_common(user, data)

        # --- Images: kept existing + newly uploaded, at least one total ---
        keep_ids = {str(i) for i in (keep_image_ids or [])}
        kept = [im for im in villa.images.all() if str(im.id) in keep_ids]
        new_images = data.images or []
        if len(kept) + len(new_images) < 1:
            raise GraphQLError("Please add at least one image.")
        if len(kept) + len(new_images) > MAX_IMAGES:
            raise GraphQLError(f"You can add up to {MAX_IMAGES} images.")

        new_files = [data_url_to_file(img) for img in new_images]

        with transaction.atomic():
            _apply_fields(villa, data, title, accepted)
            villa.save()
            _sync_blocked_dates(villa, data.blocked_dates)
            # Remove photos the user dropped (delete file + row).
            for im in villa.images.all():
                if str(im.id) not in keep_ids:
                    im.image.delete(save=False)
                    im.delete()
            new_created = [VillaImage.objects.create(villa=villa, image=f) for f in new_files]
            # Order the final set (kept + new) as the host arranged them, so the
            # chosen cover is first.
            _apply_image_order(data.image_order, data.cover_index, kept, new_created)

        villa.refresh_from_db()
        return VillaType.from_model(villa, request=info.context.request, viewer=user)

    @strawberry.mutation
    def delete_villa(self, info: strawberry.Info, id: strawberry.ID) -> bool:
        """
        Delete a villa the current user owns, along with its photos (files are
        removed from storage). Returns True on success. Requires a valid session.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=id, owner=user).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        with transaction.atomic():
            for im in villa.images.all():
                im.image.delete(save=False)  # drop the stored file (disk/Cloudinary)
                im.delete()
            villa.delete()
        return True

    @strawberry.mutation
    def toggle_favorite(self, info: strawberry.Info, villa_id: strawberry.ID) -> bool:
        """
        Add or remove a villa from the current user's wishlist. Returns the new
        state: True if now saved, False if removed. Requires a valid session.
        """
        user = require_authenticated_user(info)
        villa = Villa.objects.filter(pk=villa_id).first()
        if villa is None:
            raise GraphQLError("Villa not found.")
        fav = Favorite.objects.filter(user=user, villa=villa).first()
        if fav is not None:
            fav.delete()
            return False
        Favorite.objects.create(user=user, villa=villa)
        return True

    @strawberry.mutation
    def create_booking(self, info: strawberry.Info, data: BookingInput) -> BookingType:
        """
        Book a villa for the current user (the "Confirm and Pay" action).
        A guest may NOT book their own villa — that is rejected here as the
        single server-side enforcement gate. Totals are computed on the server.
        """
        user = require_authenticated_user(info)

        villa = Villa.objects.filter(pk=data.villa_id).first()
        if villa is None:
            raise GraphQLError("Villa not found.")

        # --- Core rule: you cannot book your own villa ---
        if villa.owner_id == user.id:
            raise GraphQLError("You cannot book your own villa.")

        # --- Dates ---
        try:
            check_in = date.fromisoformat((data.check_in or "").strip())
            check_out = date.fromisoformat((data.check_out or "").strip())
        except ValueError:
            raise GraphQLError("Please choose valid check-in and check-out dates.")
        if (check_out - check_in).days < 1:
            raise GraphQLError("Check-out must be after check-in.")
        if (check_out - check_in).days > MAX_BOOKING_NIGHTS:
            raise GraphQLError(
                f"You can book at most {MAX_BOOKING_NIGHTS} nights per stay."
            )

        # --- The host's booking window ---
        # The one rule the calendar draws (see availability.py): the villa is
        # open for `availability_days` dates starting at the first date a guest
        # could still arrive. Re-derived here from the clock, never taken from
        # the client — the page may have been open for hours.
        first_open = availability.first_bookable_date(villa)
        last_open = availability.last_bookable_date(villa)
        cutoff = availability.check_in_cutoff(villa).strftime("%I:%M %p").lstrip("0")
        if check_in < first_open:
            if check_in < availability.today_local():
                raise GraphQLError("Check-in date cannot be in the past.")
            # Today, but the door has already closed on it.
            raise GraphQLError(
                f"Check-in for {check_in.strftime('%d %b %Y')} closed at {cutoff}. "
                "Please choose a later date."
            )
        if check_in > last_open:
            raise GraphQLError(
                f"This villa is only open for bookings up to "
                f"{last_open.strftime('%d %b %Y')}. Please choose an earlier check-in date."
            )
        if check_out > availability.window_end(villa):
            raise GraphQLError(
                f"This villa is only open for bookings up to "
                f"{last_open.strftime('%d %b %Y')}. Please shorten your stay."
            )

        # --- The stay, split around nights somebody else holds ---
        # A clash in the middle no longer refuses the whole range: the villa is
        # free either side of it, so the stay breaks into runs and only the
        # nights actually slept are charged (see availability.split_stay). The
        # split is settled again inside the transaction below — this one is
        # what the guest is quoted, that one is what they're charged.
        segments, _ = availability.split_stay(villa, check_in, check_out)
        if not segments:
            raise GraphQLError(
                "Every night in those dates is already taken. "
                "Please choose different dates."
            )
        nights = availability.segment_nights(segments)

        # The page priced a specific number of nights before the guest typed a
        # card number. If availability moved since — someone cancelled and freed
        # nights the split would now swallow up — the stay in front of them is
        # not the one they agreed to, so it is re-quoted rather than charged.
        if data.expected_nights and data.expected_nights != nights:
            raise GraphQLError(
                "These dates changed while you were checking out — the stay is "
                f"now {nights} night{'' if nights == 1 else 's'}, not "
                f"{data.expected_nights}. Nothing has been charged — please "
                "check the dates and try again."
            )

        # The outer bounds bracket the runs: the guest arrives at the first and
        # finally leaves at the last, whatever they skipped in between.
        check_in, check_out = segments[0][0], segments[-1][1]

        # --- Guests: the villa's stated capacity is a hard cap ---
        guests = max(1, data.guests)
        if villa.guests and guests > villa.guests:
            raise GraphQLError(
                f"This villa sleeps up to {villa.guests} "
                f"guest{'' if villa.guests == 1 else 's'}."
            )

        # --- Payment details ---
        # Either the way this guest has paid before — nothing to retype, and
        # nothing sensitive was ever stored to retype — or a fresh method,
        # validated against what this host accepts and reduced to a masked
        # reference (see _resolve_payment), which is the same code every later
        # purchase against this booking goes through.
        saved = None
        if data.use_saved_payment:
            method, saved = _saved_payment(user, villa, data.payment_method)
            payment_reference = saved.card_last4
        else:
            method, payment_reference = _resolve_payment(villa, data)

        # --- Additional information ---
        email = (data.contact_email or "").strip()
        if "@" not in email or "." not in email:
            raise GraphQLError("Enter a valid e-mail address.")

        # --- Coupon (optional): re-resolved and applied on the server ---
        # The page's preview is advisory only; whether a code applies and how
        # much it takes off is decided here, against this villa, so a tampered
        # or since-deactivated code can't slip through.
        try:
            coupon = coupon_utils.resolve_coupon(data.coupon_code, villa)
        except coupon_utils.CouponError as exc:
            raise GraphQLError(str(exc))

        # --- Extra services (frozen server-side) ---
        # The guest sends only the NAMES they ticked; each price is taken from
        # the villa's own configured list, so a tampered client price can't get
        # through. Each service is charged per night. Unknown names are ignored.
        villa_extras = {
            str(s.get("name", "")).strip().lower(): s
            for s in (villa.extra_services or [])
            if isinstance(s, dict) and str(s.get("name", "")).strip()
        }
        chosen_extras = []
        for raw_name in (data.extra_services or []):
            key = str(raw_name or "").strip().lower()
            svc = villa_extras.get(key)
            if svc is None or key in {c["name"].lower() for c in chosen_extras}:
                continue
            chosen_extras.append(
                {"name": str(svc.get("name", "")).strip(), "price": _money(Decimal(str(svc.get("price", 0) or 0)))}
            )
        extras_per_night = sum((c["price"] for c in chosen_extras), Decimal("0.00"))
        extras_total = _money(extras_per_night * nights)
        # Store the price as a plain number (JSON), not a Decimal.
        chosen_extras = [{"name": c["name"], "price": float(c["price"])} for c in chosen_extras]

        # --- Money (frozen server-side) ---
        # The discount itself is settled inside the transaction below: whether
        # this is the guest's FIRST booking is a question two concurrent
        # checkouts could both answer "yes" to, so it is asked with their row
        # locked. Everything that doesn't depend on it is computed here.
        price = Decimal(str(villa.price_per_night))
        subtotal = _money(price * nights)
        coupon_discount = (
            coupon_utils.discount_for(coupon, subtotal) if coupon else Decimal("0.00")
        )
        service_fee = _money(subtotal * SERVICE_FEE_RATE)
        tax = _money(subtotal * TAX_RATE)

        booking = Booking(
            villa=villa,
            guest=user,
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            # Only a broken stay carries its runs; an unbroken one is fully
            # described by the two dates above (see Booking.stay_segments).
            segments=_segments_json(segments) if len(segments) > 1 else [],
            guests=guests,
            # Frozen villa snapshot — these must not change if the host later
            # edits the listing (see Booking).
            villa_title=villa.title,
            villa_city=villa.city,
            villa_country=villa.country,
            check_in_time=villa.check_in_time,
            check_out_time=villa.check_out_time,
            grace_period_minutes=villa.grace_period_minutes,
            price_per_night=price,
            subtotal=subtotal,
            # discount / coupon_code / first_booking_discount / total are all
            # settled inside the transaction below, once eligibility for the
            # welcome offer has been decided under a lock.
            service_fee=service_fee,
            tax=tax,
            extra_services=chosen_extras,
            extras_total=extras_total,
            payment_method=method,
            card_last4=payment_reference,
            # The saved card brings its own billing address — that is most of
            # what "don't make me type it again" means.
            billing_street=(saved.billing_street if saved else data.billing_street or "").strip(),
            billing_apartment=(saved.billing_apartment if saved else data.billing_apartment or "").strip(),
            billing_city=(saved.billing_city if saved else data.billing_city or "").strip(),
            billing_state=(saved.billing_state if saved else data.billing_state or "").strip(),
            billing_zip=(saved.billing_zip if saved else data.billing_zip or "").strip(),
            billing_country=(saved.billing_country if saved else data.billing_country or "").strip(),
            contact_email=email,
            contact_phone=(data.contact_phone or "").strip(),
        )

        # --- The date check, made final ---
        # Everything time-sensitive is checked AGAIN here, inside the
        # transaction and immediately before the row is written. Filling in a
        # payment form takes minutes, and two things can change underneath it:
        # the villa's check-in time can go by (so the stay's first night is no
        # longer reachable), and another guest can take the same nights.
        # Locking the villa row makes two simultaneous bookings queue rather
        # than both find the villa free.
        with transaction.atomic():
            Villa.objects.select_for_update().filter(pk=villa.pk).first()

            if check_in < availability.first_bookable_date(villa):
                raise GraphQLError(
                    f"The {cutoff} check-in time for "
                    f"{check_in.strftime('%d %b %Y')} passed while you were checking "
                    "out, so this stay can no longer start that day. Nothing has been "
                    "charged — please pick new dates."
                )

            # The split, settled for real. Re-derived under the villa's lock so
            # two checkouts racing for the same nights can't both take them —
            # and compared against what was quoted above, because a stay whose
            # nights moved is not the stay the guest agreed to pay for.
            final_segments, _ = availability.split_stay(villa, check_in, check_out)
            if final_segments != segments:
                # Name the host's own closure when that's what moved, since
                # "someone else booked it" would be wrong and unactionable.
                for starts, ends in segments:
                    closed = availability.is_blocked(villa.pk, starts, ends)
                    if closed:
                        raise GraphQLError(
                            f"The host has just closed {closed.strftime('%d %b %Y')}. "
                            "Nothing has been charged — please choose different dates."
                        )
                raise GraphQLError(
                    "Someone else booked these dates while you were checking out. "
                    "Nothing has been charged — please choose different dates."
                )

            # --- The discount, settled last ---
            # "Is this their first booking?" is a question two checkouts open in
            # two tabs could both answer yes to, so the guest's own row is
            # locked first: the second one then queues and sees the first
            # booking already committed. The welcome offer and a host's coupon
            # do not stack — the guest gets whichever is larger (see welcome.py).
            get_user_model().objects.select_for_update().filter(pk=user.pk).first()
            welcome_discount = welcome.discount_for(user, subtotal)
            winner = welcome.better_of(welcome_discount, coupon_discount)

            booking.first_booking_discount = winner == "welcome"
            booking.discount = (
                welcome_discount if winner == "welcome"
                else coupon_discount if winner == "coupon"
                else Decimal("0.00")
            )
            booking.coupon_code = coupon.code if (coupon and winner == "coupon") else ""
            # Discount comes off the accommodation; fee and tax are on the full
            # subtotal (mirrors the frontend's computeStayPricing). Extra
            # services are added straight on top — no fee or tax on them.
            booking.total = _money(
                subtotal - booking.discount + service_fee + tax + extras_total
            )

            booking.save()

        # Greet the guest on WhatsApp with the villa's photo and their trip's
        # details. Fired after the transaction commits (there is no booking to
        # announce before that) and in the background — the payment response
        # must not wait on Meta, and a failure there is not a failed booking.
        request = info.context.request
        cover = villa.cover_image_url
        if cover and not cover.startswith("http") and request is not None:
            cover = request.build_absolute_uri(cover)
        whatsapp.send_booking_confirmation(booking, cover)

        return BookingType.from_model(booking, request=request)

    @strawberry.mutation
    def cancel_booking(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """
        Cancel one of the current user's own bookings.

        The flexible cancellation policy decides both whether this is still
        allowed and what it costs (see Booking.cancellation_policy): free more
        than 24 hours before check-in, allowed but wholly non-refundable inside
        those last 24 hours, and refused from the check-in time onward. The
        penalty is frozen onto the row here, so what the guest was charged
        doesn't drift as the clock moves on.
        """
        booking = _own_booking(info, id)
        now = timezone.now()
        policy = booking.cancellation_policy(now)
        # One check for every refusal — already cancelled, already checked in,
        # or past the check-in time — each carrying the policy's own wording, so
        # the error the guest reads matches the note shown on the booking.
        if not policy.can_cancel:
            raise GraphQLError(policy.message)
        # Priced as "give up every night still held", which is what calling the
        # stay off is. On a booking already trimmed once, that is the REMAINING
        # nights and their remaining value — the guest cannot be charged twice
        # for the nights they handed back last week.
        quote = booking.nights_cancellation_quote(sorted(booking.occupied_nights()), now)
        if not quote.allowed:
            raise GraphQLError(quote.error)
        with transaction.atomic():
            booking.apply_nights_cancellation(quote, now)
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def cancel_booking_nights(
        self, info: strawberry.Info, id: strawberry.ID, nights: List[str]
    ) -> BookingType:
        """
        Give up SOME of the nights of one of the current user's own bookings.

        The guest picks dates rather than throwing the whole stay away: the
        chosen nights are priced out of the booking, refunded under the same
        sliding scale a full cancellation uses (judged per stay part), and
        handed straight back to the villa's calendar for somebody else. The
        booking stays active — shorter.

        Choosing every night still held is a whole-stay cancellation and is
        recorded as one; the guest gets there either from this screen or from
        the Cancel button, and both end in the same place.

        Re-priced HERE, at the server's clock, and never from anything the
        client sends: the quote the picker showed a minute ago may have crossed
        a tier boundary since, and what is charged has to be what is true now.
        """
        booking = _own_booking(info, id)
        now = timezone.now()
        chosen = []
        for raw in nights:
            try:
                chosen.append(date.fromisoformat(str(raw)[:10]))
            except ValueError:
                raise GraphQLError(f"'{raw}' is not a date.")
        quote = booking.nights_cancellation_quote(chosen, now)
        if not quote.allowed:
            raise GraphQLError(quote.error)
        with transaction.atomic():
            booking.apply_nights_cancellation(quote, now)
        return BookingType.from_model(booking, request=info.context.request)

    # --- Adding to a booking that is already paid for ---
    #
    # The other direction from the two above: a stay can grow as well as shrink.
    # Both mutations are the guest's own, both re-price on the server's clock at
    # the moment the button is pressed rather than trusting the quote the screen
    # was showing, and both charge for what they add — a booking never gains a
    # service or a night for free. Nothing is ever removed by either: services
    # already bought stay bought, and nights are given back through the
    # cancellation path, with its refund ladder.

    @strawberry.mutation
    def add_to_booking(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        payment: AddonPaymentInput,
        services: Optional[List[str]] = None,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        nights: Optional[List[str]] = None,
    ) -> BookingType:
        """
        Add extra services, more nights, or BOTH to one of the current user's
        own bookings — in one charge.

        One mutation and not two on purpose. A guest who adds two nights and
        breakfast has made a single decision, and splitting it would take two
        payments off their card, write two receipts, and — worse — price the
        breakfast over the stay they had rather than the one they were buying.
        Here the nights settle first and the services are quoted over the longer
        stay, which is what they will actually be delivered on.

        Everything is decided HERE, at the server's clock and inside the villa's
        lock, never from the quote the screen was showing: services must be ones
        the villa offers and this booking doesn't already have, priced from the
        villa's own list; nights must be free, inside the host's window, still
        ahead of the guest and joined to the stay. Nights the booking already
        holds are kept and not charged for.

        Nights arrive as a range (`check_in`/`check_out` — "extend up to here")
        and/or as `nights`, an explicit list, which is how a guest takes back
        single nights they had given up from inside their own span. Both are
        unioned and charged as one purchase.
        """
        booking = _own_booking(info, id)
        now = timezone.now()
        with transaction.atomic():
            # Locked for the same reason checkout locks it: two guests reaching
            # for the same night must queue, not both be sold it.
            Villa.objects.select_for_update().filter(pk=booking.villa_id).first()
            quote = addons.quote_changes(
                booking,
                services or [],
                availability.parse_date(check_in),
                availability.parse_date(check_out),
                availability.parse_dates(nights),
                now,
            )
            if not quote.allowed:
                raise GraphQLError(quote.error)
            method, reference = _addon_payment(booking, payment)
            addons.apply_changes(
                booking, quote, method=method, reference=reference, now=now
            )
        return BookingType.from_model(booking, request=info.context.request)

    # --- Check-in, in two steps ---
    #
    # A host cannot check a guest in by pressing a button: pressing it issues a
    # 4-digit PIN that appears only on the GUEST's booking page, and the host
    # has to be told it and type it back. So a recorded check-in means the guest
    # was actually standing there. Both steps are owner-only and both re-read
    # the check-in window, so a stale page can't slip past the closed gate.

    @strawberry.mutation
    def start_check_in(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """
        Step 1: issue a check-in PIN for a booking on a villa the caller owns.

        Any PIN this booking already had stops working at that moment, so
        pressing Check in again after one expires is the recovery path — there
        is never more than one live code. The digits are NOT in the response:
        they go to the guest's own booking page, and the host has to ask.
        """
        booking = _owned_booking(info, id)
        # Per PART, not per booking: a split stay is arrived at more than once,
        # and each arrival needs its own PIN.
        if booking.current_part_checked_in_at() is not None:
            raise GraphQLError("This guest is already checked in.")
        try:
            checkin.issue_pin(booking, actor=require_authenticated_user(info))
        except checkin.CheckInError as exc:
            raise GraphQLError(str(exc))
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def verify_check_in(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        pin: str,
        guests: Optional[int] = None,
    ) -> BookingType:
        """
        Step 2: the host types the PIN the guest read out, together with how
        many people are actually walking in. If the PIN matches a live code the
        guest is checked in, the headcount is recorded on the booking, and the
        PIN is spent.

        `guests` is required for a check-in and capped at the villa's capacity
        (see checkin.headcount) — optional only in the schema, so an older
        client gets the server's sentence about it rather than a type error.

        Three wrong tries lock that code and email the guest a security alert;
        the host can issue a fresh one with `startCheckIn` while the check-in
        window is still open.
        """
        booking = _owned_booking(info, id)
        try:
            checkin.verify_pin(
                booking, pin, guests=guests, actor=require_authenticated_user(info)
            )
        except checkin.CheckInError as exc:
            raise GraphQLError(str(exc))
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def allow_late_check_in(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> BookingType:
        """
        Re-open check-in on a no-show, at the host's discretion.

        The guest missed the window, so the booking stays a no-show on the
        record and the refund stays 0%; this only puts the (still PIN-verified)
        check-in button back, for the host who decides to take them in anyway.
        """
        booking = _owned_booking(info, id)
        # Per PART, not per booking: a split stay is arrived at more than once,
        # and each arrival needs its own PIN.
        if booking.current_part_checked_in_at() is not None:
            raise GraphQLError("This guest is already checked in.")
        now = timezone.now()
        if booking.lifecycle_status(now) != Booking.LIFECYCLE_NO_SHOW:
            raise GraphQLError(
                "This booking isn't a no-show — check-in is still open normally."
            )
        # The stay itself has run out: there is no longer anything to check the
        # guest into, so this isn't a decision the host still gets to make.
        if now >= booking.check_out_datetime():
            raise GraphQLError(
                "This stay has ended — the guest never checked in, and check-in "
                "can't be reopened after the check-out time."
            )
        checkin.allow_late_check_in(booking, actor=require_authenticated_user(info), now=now)
        return BookingType.from_model(booking, request=info.context.request)

    # --- Check-out, in the same two steps ---
    #
    # A departure is proved exactly like an arrival: pressing Check out issues a
    # 4-digit PIN that appears only on the GUEST's booking page, and the host has
    # to be told it. A host cannot close a stay — and cannot free up the nights a
    # guest leaves early — without the guest being there to read the code out.
    #
    # …up to the booked check-out hour, and not one step past it. From that hour
    # the code has nothing left to protect and `checkOutNow` closes the stay in
    # one press (see Booking.check_out_pin_required); half an hour later the
    # platform closes it whether anybody pressed anything or not.

    @strawberry.mutation
    def start_check_out(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """
        Step 1: issue a check-out PIN for a booking on a villa the caller owns.

        Refused unless somebody is actually checked in. Any PIN this booking
        already had stops working at that moment, so pressing Check out again
        after one expires is the recovery path.
        """
        booking = _owned_booking(info, id)
        try:
            checkin.issue_pin(
                booking,
                purpose=checkin.CHECK_OUT,
                actor=require_authenticated_user(info),
            )
        except checkin.CheckInError as exc:
            raise GraphQLError(str(exc))
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def verify_check_out(
        self, info: strawberry.Info, id: strawberry.ID, pin: str
    ) -> BookingType:
        """
        Step 2: the host types the PIN the guest read out, and the stay closes.

        On a split stay this closes the part in front of them — the guest is due
        back — so the booking is finished only when no part is left. A guest
        leaving before the hour they booked also hands those nights back to the
        calendar; no money moves either way (see checkin._record_departure).
        """
        booking = _owned_booking(info, id)
        try:
            checkin.verify_pin(
                booking,
                pin,
                purpose=checkin.CHECK_OUT,
                actor=require_authenticated_user(info),
            )
        except checkin.CheckInError as exc:
            raise GraphQLError(str(exc))
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def check_out_now(self, info: strawberry.Info, id: strawberry.ID) -> BookingType:
        """
        Close a stay whose booked check-out hour has passed — one press, no PIN.

        Refused while that hour is still ahead: checking a guest out early is
        exactly what the code exists to stop, and this is not a way around it.
        See checkin.check_out_without_pin.
        """
        booking = _owned_booking(info, id)
        try:
            checkin.check_out_without_pin(
                booking, actor=require_authenticated_user(info)
            )
        except checkin.CheckInError as exc:
            raise GraphQLError(str(exc))
        return BookingType.from_model(booking, request=info.context.request)

    @strawberry.mutation
    def submit_review(
        self,
        info: strawberry.Info,
        booking_id: strawberry.ID,
        rating: int,
        comment: str = "",
    ) -> BookingType:
        """
        Leave (or update) the guest's review for one of their COMPLETED stays.
        Guest-only, and only after check-out — you can't rate a stay that hasn't
        happened. One review per booking; submitting again edits it. Returns the
        booking so the caller's list refreshes with the new review in place.
        """
        user = require_authenticated_user(info)
        booking = Booking.objects.select_related("villa").filter(
            pk=booking_id, guest=user
        ).first()
        if booking is None:
            raise GraphQLError("Booking not found.")
        if booking.status == Booking.STATUS_CANCELLED:
            raise GraphQLError("A cancelled booking can't be reviewed.")
        # Every part checked out, not merely the booking-level stamp — see the
        # note on `can_review` in types.py. A split stay the guest is due back
        # for is not a stay they can rate yet.
        if not booking.stay_finished:
            raise GraphQLError("You can review a stay once it's completed.")
        # The parts also run out on a guest who never arrived; there is no stay
        # there to rate. Mirrors `can_review` in types.py.
        if not booking.any_part_arrived:
            raise GraphQLError("You can only review a stay you checked in for.")
        stars = int(rating)
        if stars < Review.RATING_MIN or stars > Review.RATING_MAX:
            raise GraphQLError("Please give a rating between 1 and 5 stars.")

        # Editing closes a day after the review went up. The button is gone by
        # then; this is the rule behind it, for a stale page or a hand-made
        # request. A first review is never blocked — only a change to one.
        existing = Review.objects.filter(booking=booking).first()
        if existing is not None and not existing.can_edit():
            raise GraphQLError(
                f"Reviews can be edited for {Review.EDIT_WINDOW_HOURS} hours after "
                "they're posted. This one can no longer be changed."
            )

        Review.objects.update_or_create(
            booking=booking,
            defaults={
                "villa": booking.villa,
                "guest": user,
                "rating": stars,
                "comment": (comment or "").strip()[:2000],
            },
        )
        # Re-fetch so the reverse one-to-one `review` is freshly loaded.
        booking = (
            Booking.objects.select_related("villa", "guest", "villa__owner", "review")
            .get(pk=booking.pk)
        )
        return BookingType.from_model(booking, request=info.context.request)
