from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models
from django.utils import timezone


# Standard hotel times, used when a host didn't state their own on the villa.
# They anchor the "check-in datetime" a booking is judged against (how late a
# guest is, whether they no-showed, when free cancellation ends).
DEFAULT_CHECK_IN_TIME = time(14, 0)   # 2:00 PM
DEFAULT_CHECK_OUT_TIME = time(11, 0)  # 11:00 AM


class Villa(models.Model):
    """
    A property listed by a host. Populated by the multi-step "Add your Villa"
    wizard on the frontend (Villa Details → Extra Services → Pricing → Payment).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="villas",
    )

    # --- Villa Details ---
    title = models.CharField(max_length=200)
    property_type = models.CharField(max_length=100, blank=True)  # Villa Living, Hotel…
    city = models.CharField(max_length=120, blank=True)
    country = models.CharField(max_length=120, blank=True)
    address = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    build_up_area = models.CharField(max_length=120, blank=True)  # e.g. "2000 Square Yards"

    # --- Rooms & beds ---
    # Guests book the WHOLE villa, never an individual room, so none of this is
    # inventory to draw down — it's what a guest is shown, and `guests` is the
    # cap their party size is checked against. The host states the room count
    # and the guest capacity outright; the bed breakdown is how those rooms are
    # furnished (a single bed sleeps 1, a double sleeps 2).
    bedrooms = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)
    single_bed_rooms = models.PositiveIntegerField(default=0)
    double_bed_rooms = models.PositiveIntegerField(default=0)

    # --- Availability window ---
    # How many days ahead of today this villa is open for booking. The host
    # sets it and can move it any time, so a listing is never accidentally
    # committed further out than its owner is willing to plan. Dates past the
    # window are shown to guests as not-yet-open rather than free.
    availability_days = models.PositiveIntegerField(default=5)

    # --- Facilities --- (free-form list of labels)
    services = models.JSONField(default=list, blank=True)

    # --- Extra Services --- (premium add-ons a guest can pick at booking time)
    # A list of {"name": str, "price": number} where price is charged PER NIGHT.
    # The host sets each price in the wizard; the guest ticks the ones they want
    # at checkout, and the charge is added to their total (see create_booking).
    extra_services = models.JSONField(default=list, blank=True)

    # --- House Rules ---
    # Set by the host in the wizard and shown verbatim on the villa detail page
    # — nothing here is assumed on the host's behalf. Times are optional (null =
    # the host didn't state one, so the page simply doesn't show that line); the
    # permissions default to False, i.e. not allowed unless the host says so.
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_time = models.TimeField(null=True, blank=True)
    pets_allowed = models.BooleanField(default=False)
    smoking_allowed = models.BooleanField(default=False)
    events_allowed = models.BooleanField(default=False)
    additional_rules = models.TextField(blank=True)

    # --- Pricing ---
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # --- Payment Method ---
    # Which methods guests may pay with (Mastercard, Visa, PayPal, Google Pay).
    accepted_payments = models.JSONField(default=list, blank=True)
    # The host's BANK PAYOUT details (where their earnings would be paid). The
    # account NUMBER is stored MASKED (last 4 only) — the full number is never
    # persisted. `payout_method` is legacy (used to hold "Credit/Debit Card");
    # kept for old rows and now just labelled "Bank Account".
    payout_account_name = models.CharField(max_length=120, blank=True)
    payout_bank_name = models.CharField(max_length=120, blank=True)
    payout_ifsc = models.CharField(max_length=20, blank=True)
    payout_account = models.CharField(max_length=64, blank=True)  # masked acct no.
    payout_method = models.CharField(max_length=60, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "properties_villa"
        ordering = ["-created_at"]

    # How many guests one bed of each kind sleeps.
    GUESTS_PER_SINGLE = 1
    GUESTS_PER_DOUBLE = 2

    def __str__(self):
        return f"{self.title} ({self.owner_id})"

    @property
    def cover_image_url(self) -> str:
        # The host-flagged cover, or the first image when none is flagged.
        cover = self.images.filter(is_cover=True).first() or self.images.first()
        return cover.image.url if cover else ""


class Booking(models.Model):
    """
    A guest's reservation of a villa, created from the "Confirm Payment" page.
    Totals are computed and frozen on the server at booking time; card numbers
    are stored MASKED (last 4 only) — the full PAN and CVV are never persisted.
    """

    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="bookings"
    )
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bookings",
    )

    # --- Trip details ---
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)

    # --- Frozen villa snapshot (booking time) ---
    # The villa's own fields are shown live everywhere else, but a booking must
    # not shift under the guest when the host later edits their listing — that
    # would be a bait-and-switch. The check-in/out TIMES especially: they decide
    # "late", "no-show" and the cancellation window, so they're locked to what
    # was agreed at booking. Blank/null on bookings taken before this existed,
    # which then fall back to the villa's current values (see check_in_datetime).
    villa_title = models.CharField(max_length=200, blank=True)
    villa_city = models.CharField(max_length=120, blank=True)
    villa_country = models.CharField(max_length=120, blank=True)
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_time = models.TimeField(null=True, blank=True)

    # --- Money (frozen snapshot at booking time) ---
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Coupon discount applied to this stay, and the code that granted it. Frozen
    # like the rest of the money: the coupon may later change or be deleted, but
    # what this guest actually paid never does. 0 / "" when no coupon was used.
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    coupon_code = models.CharField(max_length=32, blank=True)
    # Set when `discount` came from the platform's first-booking welcome offer
    # rather than a host's coupon. The two never both apply (see
    # properties/welcome.py), so this and `coupon_code` are mutually exclusive —
    # it's what lets a receipt say "First booking · 25% off" instead of naming a
    # code that was never used.
    first_booking_discount = models.BooleanField(default=False)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Flat tax on the accommodation subtotal. Bookings taken before tax existed
    # keep 0, so their frozen total stays exactly what the guest agreed to.
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Extra services the guest chose, frozen at booking time: a list of
    # {"name": str, "price": number} (price per night), and their summed cost
    # (price × nights). Added straight into `total` — no fee or tax on top.
    # Bookings taken before this existed keep [] / 0.
    extra_services = models.JSONField(default=list, blank=True)
    extras_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # --- Payment (masked) ---
    payment_method = models.CharField(max_length=60, blank=True)
    card_last4 = models.CharField(max_length=24, blank=True)  # "•••• 1234"

    # --- Billing address ---
    billing_street = models.CharField(max_length=255, blank=True)
    billing_apartment = models.CharField(max_length=120, blank=True)
    billing_city = models.CharField(max_length=120, blank=True)
    billing_state = models.CharField(max_length=120, blank=True)
    billing_zip = models.CharField(max_length=32, blank=True)
    billing_country = models.CharField(max_length=120, blank=True)

    # --- Additional information ---
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)

    # A booking is final the moment it's made — there is no host approval step.
    # It's active until the guest cancels or the stay has passed.
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE
    )
    # When the host marked the guest as arrived / departed. Null until they do.
    # These are the real moments the stay began and ended, recorded by the host
    # from the booking's detail view; the date fields above are only what was
    # booked. Check-out can't be set before check-in (enforced in the mutation).
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    # Set when the guest cancels. `cancellation_fee` is the late-cancellation
    # penalty charged at that moment (see cancel_fee_at) — 0 when cancelled free
    # (more than FREE_CANCEL_HOURS ahead) or not cancelled. Frozen at cancel
    # time: "now" keeps moving, but what the guest was charged does not.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "properties_booking"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Booking #{self.pk} — {self.villa_id} by {self.guest_id}"

    # --- Lifecycle, derived from the clock + the host's check-in/out stamps ---
    # The stored `status` is only active/cancelled; the states a guest and host
    # actually see ("awaiting check-in", "staying", "no-show"…) are computed
    # from the scheduled times and whether the host has checked the guest in.
    LIFECYCLE_UPCOMING = "upcoming"          # before the check-in time
    LIFECYCLE_AWAITING = "awaiting_checkin"  # check-in time reached, not yet in
    LIFECYCLE_STAYING = "staying"            # host checked the guest in
    LIFECYCLE_COMPLETED = "completed"        # host checked the guest out
    LIFECYCLE_NO_SHOW = "no_show"            # check-out passed, never arrived
    LIFECYCLE_CANCELLED = "cancelled"

    # A guest cancels free until this many hours before check-in. Inside the
    # window a fine applies, rising one step an hour to MAX_CANCEL_FINE_RATE of
    # the total right at (and after) the check-in time.
    FREE_CANCEL_HOURS = 12
    MAX_CANCEL_FINE_RATE = Decimal("0.50")

    @staticmethod
    def _aware(dt):
        if timezone.is_naive(dt):
            return timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    def scheduled_check_in_time(self):
        """The check-in time this booking is judged against: the one frozen at
        booking, else the villa's current time (legacy bookings), else 2 PM."""
        return self.check_in_time or self.villa.check_in_time or DEFAULT_CHECK_IN_TIME

    def scheduled_check_out_time(self):
        return self.check_out_time or self.villa.check_out_time or DEFAULT_CHECK_OUT_TIME

    def check_in_datetime(self):
        """When the stay is scheduled to begin — the frozen check-in time on the
        check-in date, as an aware datetime."""
        return self._aware(datetime.combine(self.check_in, self.scheduled_check_in_time()))

    def check_out_datetime(self):
        """When the stay is scheduled to end."""
        return self._aware(datetime.combine(self.check_out, self.scheduled_check_out_time()))

    def lifecycle_status(self, now=None):
        if self.status == self.STATUS_CANCELLED:
            return self.LIFECYCLE_CANCELLED
        # A real check-out / check-in the host recorded is the truth.
        if self.checked_out_at:
            return self.LIFECYCLE_COMPLETED
        if self.checked_in_at:
            return self.LIFECYCLE_STAYING
        now = now or timezone.now()
        if now < self.check_in_datetime():
            return self.LIFECYCLE_UPCOMING
        if now <= self.check_out_datetime():
            return self.LIFECYCLE_AWAITING
        return self.LIFECYCLE_NO_SHOW

    def hours_late(self, now=None):
        """How many hours past the scheduled check-in the guest still isn't
        checked in. 0 unless the booking is awaiting check-in."""
        now = now or timezone.now()
        if self.lifecycle_status(now) != self.LIFECYCLE_AWAITING:
            return 0.0
        secs = (now - self.check_in_datetime()).total_seconds()
        return max(0.0, secs / 3600.0)

    def can_cancel(self, now=None):
        """A guest may call off only a live, not-yet-arrived stay — once the host
        checks them in (staying), or the stay is over/cancelled, cancelling is
        no longer offered."""
        return self.lifecycle_status(now) in (
            self.LIFECYCLE_UPCOMING,
            self.LIFECYCLE_AWAITING,
        )

    def cancel_fee_at(self, now=None):
        """The fine a cancellation right now would carry. Free more than
        FREE_CANCEL_HOURS before check-in; inside the window it rises one step
        per whole hour, reaching MAX_CANCEL_FINE_RATE of the total at (and past)
        the check-in time."""
        now = now or timezone.now()
        hours_left = (self.check_in_datetime() - now).total_seconds() / 3600.0
        if hours_left >= self.FREE_CANCEL_HOURS:
            return Decimal("0.00")
        # Whole hours still remaining (floored toward 0; negative → 0). Each hour
        # inside the window is one step of the fine; at/after check-in it's full.
        whole_left = max(0, int(hours_left))
        hours_under = self.FREE_CANCEL_HOURS - whole_left  # 1 … 12
        rate = self.MAX_CANCEL_FINE_RATE * Decimal(hours_under) / Decimal(self.FREE_CANCEL_HOURS)
        return (Decimal(str(self.total)) * rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


class VillaBlockedDate(models.Model):
    """
    A single night the host has closed by hand.

    Separate from the rolling `availability_days` window on purpose: the window
    is "how far ahead I'm taking bookings at all" and moves with today, while
    this is "not this particular night", and it holds however far out it's set.
    A host can close a date months before the window reaches it; when the window
    does reach it, guests find it already unavailable.
    """

    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="blocked_dates"
    )
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_villa_blocked_date"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["villa", "date"], name="uniq_blocked_villa_date"
            )
        ]

    def __str__(self):
        return f"Villa {self.villa_id} blocked on {self.date}"


class VillaImage(models.Model):
    """
    One image for a villa. The file is saved through Django's configured storage
    backend, so it lands on local disk in dev and on Cloudinary in production —
    the same code path either way.
    """

    villa = models.ForeignKey(Villa, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="villas/")
    # Display order (upload order). Ties break by id.
    sort_order = models.PositiveIntegerField(default=0)
    # The cover photo — a FLAG, independent of position, so choosing a cover
    # never reorders the gallery. At most one per villa is True; falls back to
    # the first image when none is set.
    is_cover = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_villa_image"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"Image for villa {self.villa_id}"


class Coupon(models.Model):
    """
    A discount code a host offers on their villas.

    Scope is decided by `villa`: set, the code only applies to that one listing;
    null, it's a "common" coupon that applies to EVERY villa the host owns. A
    guest never sees which is which — they type the code at checkout and the
    server decides whether it applies to the villa being booked (see
    `properties.coupons`).

    `code` is stored upper-cased and is unique across the whole platform, so a
    single code entered at payment time resolves to exactly one coupon.
    """

    TYPE_PERCENT = "percent"
    TYPE_FIXED = "fixed"
    TYPE_CHOICES = [
        (TYPE_PERCENT, "Percent off"),
        (TYPE_FIXED, "Fixed amount off"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coupons",
    )
    # null = applies to all of the owner's villas ("common"); set = that villa only.
    villa = models.ForeignKey(
        Villa,
        on_delete=models.CASCADE,
        related_name="coupons",
        null=True,
        blank=True,
    )
    code = models.CharField(max_length=32, unique=True)
    discount_type = models.CharField(
        max_length=12, choices=TYPE_CHOICES, default=TYPE_PERCENT
    )
    # A percentage (1–100) when discount_type is "percent", or a currency amount
    # when "fixed". Validated in the mutation, not here.
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    active = models.BooleanField(default=True)

    # --- Validity period (both optional) ---
    # The industry-standard pair: from when the code starts working, and the
    # last day it does. Either may be left unset — blank `valid_from` means it
    # works immediately, blank `valid_until` means it never expires. Both are
    # INCLUSIVE: "valid until 30 Sep" works all day on the 30th, which is what
    # a guest reading the date expects.
    #
    # Separate from `active`: that is the host's on/off switch, this is the
    # calendar. A coupon can be switched on and still not be usable yet.
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # What a coupon is doing right now, as the host's list shows it.
    STATUS_ACTIVE = "active"        # switched on and inside its dates
    STATUS_SCHEDULED = "scheduled"  # switched on, but starts later
    STATUS_EXPIRED = "expired"      # past its last day
    STATUS_INACTIVE = "inactive"    # switched off by the host

    class Meta:
        db_table = "properties_coupon"
        ordering = ["-created_at"]

    def __str__(self):
        scope = f"villa {self.villa_id}" if self.villa_id else "all villas"
        return f"{self.code} ({scope})"

    @property
    def is_common(self) -> bool:
        """True when this coupon covers every villa the owner has (villa unset)."""
        return self.villa_id is None

    def status_on(self, today=None) -> str:
        """
        One word for the state this coupon is in. Expiry is reported ahead of
        the host's own switch: a code that has run out is expired whether or
        not they remembered to turn it off.
        """
        today = today or timezone.localdate()
        if self.valid_until and today > self.valid_until:
            return self.STATUS_EXPIRED
        if not self.active:
            return self.STATUS_INACTIVE
        if self.valid_from and today < self.valid_from:
            return self.STATUS_SCHEDULED
        return self.STATUS_ACTIVE

    def is_live(self, today=None) -> bool:
        """Can a guest use this code today?"""
        return self.status_on(today) == self.STATUS_ACTIVE


class Favorite(models.Model):
    """A villa a user has saved to their wishlist. One row per (user, villa)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorites",
    )
    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="favorited_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_favorite"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "villa"], name="uniq_favorite_user_villa"
            )
        ]

    def __str__(self):
        return f"{self.user_id} ♥ villa {self.villa_id}"


class Review(models.Model):
    """
    A guest's rating + written review of a villa.

    A real guest may only review a stay they COMPLETED (checked out), and once
    per booking — that's enforced in the mutation and by the one-to-one
    `booking`. Seeded/demo reviews carry no booking (null), so the field is
    nullable; Postgres allows many null one-to-ones.
    """

    RATING_MIN = 1
    RATING_MAX = 5

    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="reviews"
    )
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews",
    )
    # The completed booking this review is for. One review per booking. Null for
    # seeded demo reviews that aren't tied to a real stay.
    booking = models.OneToOneField(
        Booking,
        on_delete=models.CASCADE,
        related_name="review",
        null=True,
        blank=True,
    )
    rating = models.PositiveSmallIntegerField(default=5)  # 1..5 stars
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "properties_review"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.rating}★ by {self.guest_id} on villa {self.villa_id}"
