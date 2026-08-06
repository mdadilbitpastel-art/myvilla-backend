import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import ceil

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime


# Standard hotel times, used when a host didn't state their own on the villa.
# They anchor the "check-in datetime" a booking is judged against (how late a
# guest is, whether they no-showed, when free cancellation ends).
DEFAULT_CHECK_IN_TIME = time(14, 0)   # 2:00 PM
DEFAULT_CHECK_OUT_TIME = time(11, 0)  # 11:00 AM

# How long past the check-in time a host may still check a guest in, when the
# property hasn't set its own. Configurable per villa (Villa.grace_period_
# minutes) and frozen onto each booking as it's made.
DEFAULT_GRACE_MINUTES = 120
# The closing stretch of that window, where the check-in button goes yellow to
# say "still allowed, but not for much longer".
GRACE_WARNING_MINUTES = 60

# How long past the booked check-out hour a stay is left open before the
# platform closes it itself.
#
# The departure PIN exists for ONE reason: so a host cannot put a guest out
# before the hour they paid for. Past that hour there is nothing left to
# protect — the guest's time is up either way — and a stay left open because
# nobody pressed a button is a stay that goes on reporting a guest in a property
# they left, blocking the calendar and the review, sometimes for days. So the
# half hour is the guest's grace to actually walk out, and after it the stay
# closes without a code, on the clock alone.
FORCED_CHECK_OUT_MINUTES = 30


# What a guest is told about a property whose host has taken it down, and what
# the host is told about their own. The two differ on purpose: one is a fact
# about the listing, the other is a fact about something the reader DID.
MSG_VILLA_REMOVED_GUEST = "This property is no longer listed on MyVilla."
MSG_VILLA_REMOVED_OWNER = "You removed this property from MyVilla."


# What each side is told when a guest never turned up. The arrival window
# closing cancels the whole booking on its own (see `Booking.sync_no_show`) —
# there is no decision left for anybody to make about it, so both readers are
# told what has already happened rather than offered something to do.
#
# Worded from where each of them stands: the host's sentence is about their
# property and their calendar, the guest's is about their money. Neither is
# shown the other's — a host does not need to be told what the guest was
# refunded, and a guest reading "your nights are back on the calendar" would
# think the sentence was meant for somebody else.
MSG_NO_SHOW_OWNER = (
    "The guest never checked in, so this booking was cancelled automatically "
    "when the arrival window closed. Nothing is refunded to them."
)
MSG_NO_SHOW_GUEST = (
    "You did not check in before the arrival window closed, so this booking "
    "was cancelled. A missed arrival is not refundable, so nothing comes back."
)


class VillaQuerySet(models.QuerySet):
    """
    Villas are never really deleted — a host taking one down must not erase the
    stays booked on it, which go on being checked in and out and are still the
    receipt for money that changed hands. `delete_villa` stamps `deleted_at`
    instead, and everything that OFFERS a villa to somebody asks for `.live()`.

    The plain manager still answers with every row on purpose: a booking, a
    review or a receipt reaches its villa through a foreign key, and those must
    keep resolving long after the listing is gone.
    """

    def live(self):
        """Listings still on the platform — what search, the home page and the
        booking path may show."""
        return self.filter(deleted_at__isnull=True)

    def removed(self):
        """Listings their host has taken down."""
        return self.filter(deleted_at__isnull=False)


@dataclass(frozen=True)
class CheckInGate:
    """
    The host's check-in button, as the server sees it — the single description
    of a booking's check-in state that the API, the dashboard button and the
    mutations all read, so none of them can disagree about whether check-in is
    open right now.

        before the check-in time  → grey, disabled ("Check-in opens …")
        first part of the window  → green, enabled
        closing stretch           → yellow, enabled (grace-period warning)
        window shut, no arrival   → hidden; the booking is a No Show
        host allowed a late one   → green again, by the host's own decision

    `button_state` is one of "grey" / "green" / "yellow" / "hidden".
    """

    booking_status: str
    button_visible: bool
    button_state: str
    checkin_available: bool
    grace_period_remaining_minutes: int
    otp_required: bool
    message: str
    opens_at: datetime
    grace_ends_at: datetime


@dataclass(frozen=True)
class CheckOutGate:
    """
    The departure half of the same idea (see CheckInGate): whether the host may
    check this guest out right now, and what to tell both sides about it.

    Check-out has no window to miss — a guest may leave whenever they like — so
    there is only one thing to decide (is somebody actually checked in?) and one
    thing worth saying, which is what leaving NOW would cost. A departure before
    the booked hour hands the unused nights back to the calendar and refunds
    nothing, and `early` / `released_nights` are what let the host be told that
    before they verify the PIN rather than after.
    """

    available: bool
    message: str
    early: bool
    released_nights: int
    # The date the current part would end on if the guest left now — the first
    # night that goes back on sale is this one.
    ends_on: date


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
    property_type = models.CharField(max_length=100, blank=True)  # Villa Living, Bungalow…
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
    # How long after the check-in time the host may still check a guest in.
    # Once it runs out the stay is a no-show and the button goes away — see
    # Booking.check_in_gate. Per-property so a host who expects late arrivals
    # (a remote place, a late flight route) can allow for them.
    grace_period_minutes = models.PositiveIntegerField(default=120)
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

    # When the host took this listing down, or NULL while it is still listed.
    # A removal is a soft one — see VillaQuerySet — because the bookings already
    # made on the property must run to their end, and the photo, the title and
    # the address they were made against have to keep resolving for as long as
    # anybody can look the stay up.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = VillaQuerySet.as_manager()

    class Meta:
        db_table = "properties_villa"
        ordering = ["-created_at"]

    # How many guests one bed of each kind sleeps.
    GUESTS_PER_SINGLE = 1
    GUESTS_PER_DOUBLE = 2

    def __str__(self):
        return f"{self.title} ({self.owner_id})"

    @property
    def is_deleted(self) -> bool:
        """Whether the host has taken this listing down."""
        return self.deleted_at is not None

    def soft_delete(self, now=None) -> bool:
        """
        Take this listing off the platform without losing it. Returns False if
        it was already down, so a double-press doesn't move the date.

        Nothing else is touched — not the photos, not the bookings, not the
        reviews. That is the whole point: a stay on this villa carries on
        exactly as it was, and only the ways IN to booking a new one close.
        """
        if self.deleted_at is not None:
            return False
        self.deleted_at = now or timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])
        return True

    def removal_message(self, *, for_owner: bool) -> str:
        """The line to show about this villa's removal, from the reader's side
        — "" while it is still listed."""
        if not self.is_deleted:
            return ""
        return MSG_VILLA_REMOVED_OWNER if for_owner else MSG_VILLA_REMOVED_GUEST

    @property
    def cover_image_url(self) -> str:
        # The host-flagged cover, or the first image when none is flagged.
        cover = self.images.filter(is_cover=True).first() or self.images.first()
        return cover.image.url if cover else ""


@dataclass(frozen=True)
class CancellationPolicy:
    """
    What cancelling a booking at one particular moment would mean.

    Produced by `Booking.cancellation_policy()`; it is the single source of
    truth for the cancel button, the confirmation copy, the API fields and the
    fine actually charged, so none of them can drift apart from the others.

    `refund_percentage` + `penalty_percentage` always make 100, and
    `refund_amount` + `penalty_amount` always make the booking total exactly
    (the refund is the remainder, so rounding never loses or invents a rupee).
    """

    can_cancel: bool
    refund_percentage: int
    penalty_percentage: int
    message: str
    refund_amount: Decimal
    penalty_amount: Decimal

    @classmethod
    def build(cls, total, *, can_cancel: bool, refund_percentage: int, message: str):
        """Split `total` by `refund_percentage` and pair it with its copy."""
        total = Decimal(str(total or 0))
        penalty_percentage = 100 - refund_percentage
        penalty = (total * Decimal(penalty_percentage) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return cls(
            can_cancel=can_cancel,
            refund_percentage=refund_percentage,
            penalty_percentage=penalty_percentage,
            message=message,
            refund_amount=total - penalty,
            penalty_amount=penalty,
        )


@dataclass(frozen=True)
class NightsCancellationQuote:
    """
    What giving up a chosen set of nights would mean, priced and checked.

    Produced by `Booking.nights_cancellation_quote()` and used three times over:
    to draw the guest's date picker, to answer the live "what do I get back?"
    query behind it, and to perform the cancellation itself. One calculation, so
    the figure quoted and the figure charged cannot disagree.

    `allowed` is False when the selection can't be cancelled at all, and `error`
    then says why in the guest's words. `full` means the selection empties the
    booking — every night still held — which is an ordinary whole-stay
    cancellation arrived at from the other direction.
    """

    nights: tuple            # the dates being given up, in order
    stay_value: Decimal      # what those nights are worth of the total
    penalty_amount: Decimal  # kept by the policy
    refund_amount: Decimal   # handed back
    refund_percentage: int   # of `stay_value`, for display
    full: bool
    allowed: bool
    message: str
    error: str = ""
    # Of `stay_value`, what was extra services on those nights — and it comes
    # back IN FULL, whatever the cancellation ladder charges on the stay itself.
    # A service is something the host was going to do on a night that is no
    # longer happening: there is nothing to keep a percentage of. Part of
    # `refund_amount`, called out separately so the guest can see why the
    # refund is more than the tier alone would give.
    extras_value: Decimal = Decimal("0.00")
    # Of `stay_value`, the platform fee on those nights — kept in full, whatever
    # the ladder allows on the accommodation. Part of `penalty_amount`, named
    # separately so a refund smaller than the tier suggests explains itself.
    service_fee: Decimal = Decimal("0.00")

    @property
    def nights_count(self) -> int:
        return len(self.nights)


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
    # `check_in`/`check_out` are the OUTER bounds of the stay: the day the guest
    # arrives and the day they finally leave. `nights` is what they actually
    # sleep — and the two only agree when the stay is one unbroken run.
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)

    # --- Split stays ---
    # A guest may book across nights somebody else already holds: the villa is
    # theirs either side of the clash, and they simply move out and back in
    # again. Those runs are stored here as [{"check_in": iso, "check_out": iso},
    # …] in date order, and they are the truth about which nights this booking
    # occupies — `check_in`/`check_out` above only bracket them.
    #
    # Left EMPTY for the ordinary unbroken stay (and for every booking taken
    # before splitting existed), which is why every reader goes through
    # `stay_segments()` rather than this field: it falls back to the single
    # check_in→check_out run, so one code path covers both.
    segments = models.JSONField(default=list, blank=True)

    # Arrival and departure PER PART, as
    # [{"index": 1, "checked_in_at": iso, "checked_out_at": iso}, …].
    #
    # A split stay is checked in and out once per part — the guest really does
    # leave and come back, so closing part 1 must not close the booking. The two
    # columns further down keep their plain meanings (the FIRST arrival and the
    # FINAL departure); this is the detail underneath them, and it is what the
    # check-in window and the lifecycle are actually judged against.
    #
    # Empty for an unbroken stay, whose single part is fully described by those
    # two columns — which is why every reader goes through `part_stays()`.
    segment_stays = models.JSONField(default=list, blank=True)

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
    # Frozen with the times above, and for the same reason: how long this guest
    # has to arrive was part of the deal. Null on bookings taken before the
    # grace period existed, which fall back to the villa's current setting.
    grace_period_minutes = models.PositiveIntegerField(null=True, blank=True)

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
    # How many people the host actually counted through the door, recorded with
    # the arrival they verified. `guests` up top is only what was BOOKED months
    # earlier — parties turn up short, or with a cousin nobody mentioned — and
    # the host is the one person standing there able to say. Null until someone
    # is checked in, which is what keeps "never recorded" distinguishable from
    # a headcount that happens to match the booking.
    #
    # Never more than the villa sleeps: the dialog offers the villa's capacity
    # as the ceiling and the server holds them to it (see checkin.verify_pin).
    # On a split stay this is the LATEST arrival's count — the guest leaves and
    # comes back, possibly with a different party, and each part's own figure
    # is kept in `segment_stays`.
    checked_in_guests = models.PositiveIntegerField(null=True, blank=True)
    # LEGACY. Check-out used to be a checklist the host ticked ("keys returned",
    # "property inspected"); it is now PIN-verified like check-in, so nothing
    # writes these any more. They are kept, unread, so the stays that were closed
    # that way don't lose what was recorded about them.
    check_out_checklist = models.JSONField(default=list, blank=True)
    check_out_notes = models.TextField(blank=True)
    # Nights handed back to the calendar by an early departure — the guest left
    # before the hour they booked, so the nights they didn't use go back on sale
    # for somebody else (see `departure_release`). No money moves: the stay was
    # paid for in full and leaving early is the guest's own choice, so this is a
    # count of released NIGHTS, never of a refund. 0 on a stay run to its end.
    released_nights = models.PositiveIntegerField(default=0)
    # Set when the guest cancels. `cancellation_fee` is the penalty charged at
    # that moment (see cancellation_policy) — 0 when cancelled more than 24
    # hours before check-in, the whole total inside that window. Frozen at
    # cancel time: "now" keeps moving, but what the guest was charged does not.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Stamped the first time this booking is looked at after its check-in grace
    # period ran out with nobody checked in (see `sync_no_show`). The no-show
    # STATE is derived from the clock — it would be true whether or not anything
    # had written it down — but the moment it happened is worth keeping: it's
    # what a host, a support agent or a payout dispute needs months later.
    no_show_at = models.DateTimeField(null=True, blank=True)
    # Set when the PLATFORM closed this stay rather than the host: the booked
    # check-out hour came and went, the half-hour grace ran out with nobody
    # checked out, and it was closed on the clock alone (see
    # checkin.sync_forced_check_out). Stamped with the DEADLINE, not the moment
    # somebody happened to load the page — the stay ended when it ended.
    #
    # `checked_out_at` carries the same instant, so every reader that only asks
    # "is this stay over?" needs to know nothing about this. What it adds is the
    # answer to "who ended it?", which is the question anyone reviewing a
    # disputed departure months later is actually asking.
    forced_check_out_at = models.DateTimeField(null=True, blank=True)
    # There was a `late_check_in_allowed` here: the host's decision to take a
    # no-show guest in after the window had closed. It has gone, along with the
    # button that set it. A window that can be re-opened by whoever is standing
    # at the desk is not a window, and while it stayed open the villa stayed off
    # the market for a guest who was not coming. The window now closes once and
    # closes the booking with it (see `sync_no_show`).
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

    # --- Flexible cancellation policy ---
    # A sliding scale, decided by how far `now` sits from this booking's own
    # check-in DATETIME (its check-in date at the check-in time frozen when the
    # guest booked). The nearer the stay, the less the host can do about the
    # empty nights, so the less comes back — the standard travel-industry slab:
    #
    #   15 days or more before check-in → 100% back, free
    #   7–15 days                       →  50% back, 50% charge
    #   3–7 days                        →  25% back, 75% charge
    #   24 hours–3 days                 →  10% back, 90% charge
    #   inside the last 24 hours        →   0% back — still cancellable, but the
    #                                       stay is non-refundable
    #   at/after the check-in time      → cancelling is closed, 0% back
    #
    # Every boundary is a moment, never a date: for a 2 PM check-in, 1:59 PM the
    # day before is already inside the last 24 hours, and 2:01 PM three days out
    # has just crossed into the 50% band. A boundary landed on exactly gives the
    # guest the kinder side of it — 7 days out is the 90% band, not the 50%.
    #
    # And the moment measured to is the moment of the NIGHT being given up, not
    # the start of the stay (see `night_refund_tiers`). A guest handing back the
    # last night of a fortnight's stay has given a fortnight's notice on it,
    # whatever the notice on their arrival happens to be.
    NO_REFUND_WINDOW_HOURS = 24
    REFUND_AFTER_CHECK_IN = 0

    MSG_FREE = "Free cancellation available."
    MSG_NO_REFUND = (
        "Cancelling within 24 hours of check-in is non-refundable — "
        "no refund will be issued."
    )

    # (hours before check-in, refund %, the line the guest is shown) — ordered
    # from the most distant band to the nearest, and read top-down, so the first
    # threshold `now` clears is the band it is in. The single source of the
    # ladder: the whole-stay policy, a partial cancellation's per-part pricing
    # and the API's quoted figures all come through `refund_tier_at` below.
    REFUND_TIERS = (
        (15 * 24, 100, MSG_FREE),
        (7 * 24, 50, "Cancelling now carries a 50% charge — half is refunded."),
        (3 * 24, 25, "Cancelling now carries a 75% charge — 25% is refunded."),
        (NO_REFUND_WINDOW_HOURS, 10, "Cancelling now carries a 90% charge — 10% is refunded."),
        (0, 0, MSG_NO_REFUND),
    )
    MSG_EXPIRED = "Cancellation period has expired."
    MSG_CHECKED_IN = "You're already checked in — this stay can no longer be cancelled."
    MSG_ALREADY_CANCELLED = "This booking is already cancelled."

    @staticmethod
    def _aware(dt):
        if timezone.is_naive(dt):
            return timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    # --- The nights this booking actually holds ---

    def stay_segments(self):
        """
        The stay as a list of (check_in, check_out) date pairs, in order.

        One pair for an ordinary stay; several when the guest booked around
        nights somebody else held. This is the ONLY thing that should be asked
        which nights a booking occupies — reading `check_in`…`check_out`
        directly would swallow the gaps, and a gap belongs to another guest.
        """
        runs = []
        for raw in self.segments or []:
            if not isinstance(raw, dict):
                continue
            try:
                start = date.fromisoformat(str(raw.get("check_in", ""))[:10])
                end = date.fromisoformat(str(raw.get("check_out", ""))[:10])
            except ValueError:
                continue
            if end > start:
                runs.append((start, end))
        # No stored split (the ordinary case, and every legacy booking): the
        # stay is the single run its outer dates describe.
        if not runs:
            return [(self.check_in, self.check_out)]
        return sorted(runs)

    def occupied_nights(self):
        """Every night this booking sleeps in, as a set of dates."""
        nights = set()
        for start, end in self.stay_segments():
            night = start
            while night < end:
                nights.add(night)
                night += timedelta(days=1)
        return nights

    @property
    def is_split(self) -> bool:
        """True when the stay is broken by nights another guest holds."""
        return len(self.stay_segments()) > 1

    def stay_segment_windows(self):
        """
        Each run of the stay as (check_in, check_out, starts_at, ends_at) — the
        two dates, and the wall-clock moments the guest may arrive and must
        leave on each of them.

        The times are this booking's own frozen ones, so every part of a split
        stay opens and closes on the hours that were agreed when it was booked,
        exactly like the stay as a whole. This is what lets the guest be told
        "back on 1 Aug at 2:00 PM" rather than just "1 Aug", which on a stay
        they have to vacate and return to is the part that actually matters.
        """
        arrive = self.scheduled_check_in_time()
        leave = self.scheduled_check_out_time()
        return [
            (
                start,
                end,
                self._aware(datetime.combine(start, arrive)),
                self._aware(datetime.combine(end, leave)),
            )
            for start, end in self.stay_segments()
        ]

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

    # --- Arrival and departure, part by part ---
    #
    # A split stay is lived in instalments: the guest checks in, checks out when
    # somebody else's nights begin, and checks back in afterwards. So "is this
    # guest checked in?" and "when does check-in open?" are questions about the
    # part in front of us, not about the booking as a whole — and everything
    # below answers them for the CURRENT part. On an unbroken stay there is only
    # ever one part, so each of these collapses to the plain booking-level
    # answer it always gave.

    def part_stays(self) -> dict:
        """Per-part arrival/departure stamps, keyed by 1-based part index."""
        out = {}
        for raw in self.segment_stays or []:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            out[index] = {
                "checked_in_at": self._parse_stamp(raw.get("checked_in_at")),
                "checked_out_at": self._parse_stamp(raw.get("checked_out_at")),
                "guests": self._parse_count(raw.get("guests")),
                # Split off a part the guest had already begun (see drop_nights):
                # nobody has arrived for THIS run, but the money on it is spent.
                "under_way": bool(raw.get("under_way")),
            }
        # A stay that predates per-part stamps (or was never split) carries its
        # arrival and departure on the booking itself — that IS part one's.
        if not out and (self.checked_in_at or self.checked_out_at):
            out[1] = {
                "checked_in_at": self.checked_in_at,
                "checked_out_at": self.checked_out_at,
                "guests": self.checked_in_guests,
                "under_way": False,
            }
        return out

    @staticmethod
    def _parse_stamp(value):
        if not value:
            return None
        parsed = parse_datetime(str(value))
        return Booking._aware(parsed) if parsed else None

    @staticmethod
    def _parse_count(value):
        """A headcount out of the JSON, or None — a part checked in before the
        host was asked for one has no number to report, not a zero."""
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return count if count > 0 else None

    def guest_capacity(self) -> int:
        """
        The most people this villa sleeps — the ceiling on an arrival headcount.

        Read LIVE off the listing rather than frozen at booking time like the
        prices and hours: those are terms that were agreed, but how many beds
        the property has is a fact about the building, and the host counting
        people in at the door needs today's answer.
        """
        return max(1, int(getattr(self.villa, "guests", 0) or 1))

    def record_part_stay(
        self, index: int, *, checked_in_at=None, checked_out_at=None, guests=None
    ):
        """Stamp one part's arrival or departure, in place. Caller saves."""
        rows = [dict(r) for r in (self.segment_stays or []) if isinstance(r, dict)]
        row = next((r for r in rows if str(r.get("index")) == str(index)), None)
        if row is None:
            row = {"index": int(index)}
            rows.append(row)
        if checked_in_at is not None:
            row["checked_in_at"] = checked_in_at.isoformat()
        if checked_out_at is not None:
            row["checked_out_at"] = checked_out_at.isoformat()
        if guests is not None:
            row["guests"] = int(guests)
        rows.sort(key=lambda r: int(r.get("index") or 0))
        self.segment_stays = rows

    def current_part(self, now=None):
        """
        The part of the stay in front of us: the first one still outstanding.

        A part stops being outstanding two ways. Either it was closed off — the
        host checked the guest out of it — or nobody ever arrived for it and its
        own check-out hour has passed, so there is no longer anything to arrive
        for or to vacate. Both are behind us, and neither says a word about the
        parts still to come: a guest who missed part one is still due back for
        part two, and everything that reads this must be looking at part two by
        then, or they could never be let in at all.

        None once every part is behind us — which is the moment, and the only
        moment, the whole stay is over.
        """
        now = now or timezone.now()
        stays = self.part_stays()
        for index, (start, end, opens_at, ends_at) in enumerate(
            self.stay_segment_windows(), start=1
        ):
            stay = stays.get(index, {})
            if stay.get("checked_out_at") is not None:
                continue
            # Never arrived and the hour to vacate has been and gone. Note this
            # is the part's END, not the end of its check-in window: past the
            # grace period the host may still take a late arrival in by hand
            # (see `check_in_gate`), and that has to stay possible.
            if stay.get("checked_in_at") is None and now >= ends_at:
                continue
            return {
                "index": index,
                "total": len(self.stay_segments()),
                "check_in": start,
                "check_out": end,
                "opens_at": opens_at,
                "ends_at": ends_at,
                "checked_in_at": stay.get("checked_in_at"),
            }
        return None

    def stay_over(self, now=None) -> bool:
        """No part of this stay is outstanding any more — see `current_part`."""
        return self.current_part(now) is None

    @property
    def stay_finished(self) -> bool:
        """Every part behind us — the stay is genuinely over."""
        return self.stay_over()

    @property
    def any_part_arrived(self) -> bool:
        """The guest was checked in for at least one part of this stay.

        What separates a finished stay from a no-show once every part is behind
        us: the parts run out either way, but only one of them was actually
        lived in.
        """
        if self.checked_in_at:
            return True
        return any(s.get("checked_in_at") for s in self.part_stays().values())

    def current_part_checked_in_at(self, now=None):
        """When the guest arrived for the part in front of us, or None."""
        part = self.current_part(now)
        return part["checked_in_at"] if part else None

    def current_part_opens_at(self, now=None):
        """When check-in opens for the part in front of us."""
        part = self.current_part(now)
        return part["opens_at"] if part else self.check_in_datetime()

    def current_part_ends_at(self, now=None):
        """When the part in front of us must be vacated."""
        part = self.current_part(now)
        return part["ends_at"] if part else self.check_out_datetime()

    def current_part_grace_ends_at(self, now=None):
        """When the part in front of us stops being arrivable-for."""
        return self.current_part_opens_at(now) + timedelta(
            minutes=self.scheduled_grace_minutes()
        )

    def auto_check_out_at(self, now=None):
        """
        When this stay closes itself — the part in front of us must be vacated,
        plus the half hour the guest gets to actually leave. None when there is
        nothing for a forced check-out to close.

        Only ever set for a part somebody CHECKED INTO. A part nobody arrived
        for isn't a guest overstaying, it's a no-show, and that has its own
        ending; forcing a check-out on it would record a departure that never
        happened.
        """
        now = now or timezone.now()
        part = self.current_part(now)
        if part is None or part["checked_in_at"] is None:
            return None
        return part["ends_at"] + timedelta(minutes=FORCED_CHECK_OUT_MINUTES)

    def auto_check_out_seconds_left(self, now=None) -> int:
        """
        Seconds until this stay closes itself, 0 once it is due (or when nothing
        is open to close). Counted in seconds, not minutes: what the guest and
        host are shown in the last half hour is a clock running down, and a
        reading that only moves once a minute reads as frozen.
        """
        now = now or timezone.now()
        due_at = self.auto_check_out_at(now)
        if due_at is None:
            return 0
        return max(0, int((due_at - now).total_seconds()))

    def check_out_overdue(self, now=None) -> bool:
        """The hour to vacate has passed and nobody has closed the stay — the
        window in which the countdown to a forced check-out is running."""
        now = now or timezone.now()
        part = self.current_part(now)
        if part is None or part["checked_in_at"] is None:
            return False
        return now >= part["ends_at"]

    def check_out_pin_required(self, now=None) -> bool:
        """
        Whether the host still has to be told the guest's PIN to close this
        stay.

        The departure code exists for exactly one reason: to stop a host putting
        a guest out BEFORE the hour they paid to stay until. Once that hour has
        passed there is nothing left for it to protect — the guest owes the
        property nothing more, and the platform is itself going to close the
        stay half an hour later without asking anybody (see
        `sync_forced_check_out`). Demanding a code in between would only mean a
        host who is standing in an empty villa can't record what plainly already
        happened, while the clock ends up recording it for them.

        So: PIN before the hour, no PIN after it. The same reading the forced
        check-out is built on, asked one step earlier.
        """
        return not self.check_out_overdue(now)

    def scheduled_grace_minutes(self) -> int:
        """How long after check-in time the host may still take this guest in:
        the grace period frozen at booking, else the villa's current setting."""
        if self.grace_period_minutes is not None:
            return int(self.grace_period_minutes)
        return int(getattr(self.villa, "grace_period_minutes", None) or DEFAULT_GRACE_MINUTES)

    def grace_ends_at(self):
        """The moment the check-in window shuts and the stay becomes a no-show."""
        return self.check_in_datetime() + timedelta(minutes=self.scheduled_grace_minutes())

    def lifecycle_status(self, now=None):
        if self.status == self.STATUS_CANCELLED:
            return self.LIFECYCLE_CANCELLED
        now = now or timezone.now()
        # Judged on the part in front of us, never on the booking's outer
        # dates: closing part one of a split stay ends that part, not the stay,
        # and the guest is due back for the next one. A settled state — the two
        # the guest's booking history is built from — is therefore only reachable
        # once NO part is outstanding.
        part = self.current_part(now)
        if part is None:
            # The parts have run out. Which way it ended is not the clock's to
            # say: a stay somebody lived in is complete, one nobody ever turned
            # up for is a no-show, however many parts it was cut into.
            return (
                self.LIFECYCLE_COMPLETED
                if self.any_part_arrived
                else self.LIFECYCLE_NO_SHOW
            )
        if part["checked_in_at"]:
            return self.LIFECYCLE_STAYING
        if now < part["opens_at"]:
            return self.LIFECYCLE_UPCOMING
        # The check-in window: open from the check-in time until the grace
        # period runs out. Past that with nobody checked in, it's a no-show —
        # the window closing is what decides that, not the stay's end date.
        if now < self.current_part_grace_ends_at(now):
            return self.LIFECYCLE_AWAITING
        return self.LIFECYCLE_NO_SHOW

    def no_show_message(self, *, for_owner: bool) -> str:
        """The line to show about a stay nobody arrived for, from the reader's
        own side — "" on a booking that isn't one."""
        if self.no_show_at is None:
            return ""
        return MSG_NO_SHOW_OWNER if for_owner else MSG_NO_SHOW_GUEST

    def no_show_cancellation(self) -> "NightsCancellationQuote":
        """
        The receipt for a stay the guest never came to: every night still held,
        given up, nothing back.

        Built here rather than through `nights_cancellation_quote` because that
        one prices a DECISION — it asks how far ahead of check-in we are and
        pays the ladder accordingly, and refuses nights that have already begun.
        A no-show is neither. The window has shut, the first night has started
        and is spent, and the ladder's whole subject — how much notice the host
        got — is answered by "none at all". So the figure is not negotiated: the
        nights are worth what is left of the booking, and all of it is kept.

        Extra services go with them and are not refunded either, for the same
        reason the nights aren't: the host stood ready to deliver them.
        """
        held = sorted(self.occupied_nights())
        value = self.nights_value(held, empties=True)
        return NightsCancellationQuote(
            nights=tuple(held),
            stay_value=value,
            penalty_amount=value,
            refund_amount=Decimal("0.00"),
            refund_percentage=0,
            full=True,
            allowed=True,
            # The receipt is the money's own record, and the money is the
            # guest's side of this — so it carries the guest's wording.
            message=MSG_NO_SHOW_GUEST,
            extras_value=Decimal("0.00"),
        )

    def sync_no_show(self, now=None) -> bool:
        """
        Close a booking whose arrival window shut with nobody in it.

        The no-show STATE is derived — `lifecycle_status` reads it off the clock
        whether or not anything wrote it down — but what FOLLOWS from it is not
        derivable, so it is carried out here: the moment is stamped, the whole
        booking is cancelled, and no refund is issued. A guest who never came
        does not keep the villa off the market for the rest of the week, and the
        host is not left holding a stay that can only ever end one way while a
        button asks them to decide about it.

        Every night goes at once, the booking's outer dates included. The nights
        already behind us were never going to be sold again anyway — the villa's
        booking window has moved past today's check-in hour by the time this can
        run (see availability.first_bookable_date), which is exactly the hour
        that made this a no-show. So what actually returns to the calendar is
        every night still ahead, and only those.

        Written lazily, the first time the booking is looked at after the window
        shut — that avoids a scheduler for something nobody is waiting on at the
        stroke of the hour. `availability.booked_nights` looks too, so the dates
        re-open for other guests without anyone opening the booking itself.

        Returns True when it closed something (so callers can log it once).
        """
        now = now or timezone.now()
        if self.status != self.STATUS_ACTIVE:
            return False
        # Somebody DID arrive, for some part of this stay. Whatever the current
        # part reports, this is not a booking nobody turned up for, and taking
        # away a stay that was lived in is not this method's business.
        if self.any_part_arrived:
            return False
        if self.lifecycle_status(now) != self.LIFECYCLE_NO_SHOW:
            return False
        with transaction.atomic():
            if self.no_show_at is None:
                # The moment it BECAME true, not the moment somebody looked —
                # a booking first opened three days late did not no-show today.
                self.no_show_at = self.current_part_grace_ends_at()
                self.save(update_fields=["no_show_at", "updated_at"])
            self.apply_nights_cancellation(self.no_show_cancellation(), now)
        return True

    def check_in_gate(self, now=None) -> "CheckInGate":
        """
        Whether the host may check this guest in right now, and how the button
        should look while they can't. See CheckInGate for the states.
        """
        now = now or timezone.now()
        # The part in front of us: on a split stay each one opens and closes on
        # its own hours, so the button comes back for part two rather than
        # disappearing when part one is closed off — or missed.
        opens_at = self.current_part_opens_at(now)
        grace_ends = self.current_part_grace_ends_at(now)
        remaining = max(0, ceil((grace_ends - now).total_seconds() / 60))

        def gate(status, *, visible, state, available, message, otp=False):
            return CheckInGate(
                booking_status=status,
                button_visible=visible,
                button_state=state,
                checkin_available=available,
                grace_period_remaining_minutes=remaining,
                otp_required=otp,
                message=message,
                opens_at=opens_at,
                grace_ends_at=grace_ends,
            )

        if self.status == self.STATUS_CANCELLED:
            return gate("Cancelled", visible=False, state="hidden", available=False,
                        message="This booking was cancelled.")
        if self.stay_over(now):
            # Nothing outstanding. Either it was lived in and closed off, or
            # nobody ever came — and the host reading this button wants to be
            # told which, not "complete" for a guest who never arrived.
            if self.any_part_arrived:
                return gate("Completed", visible=False, state="hidden", available=False,
                            message="The stay is complete.")
            return gate(
                "No Show", visible=False, state="hidden", available=False,
                message="The guest never checked in and the stay has now ended.",
            )
        if self.current_part_checked_in_at(now):
            return gate("Checked In", visible=False, state="hidden", available=False,
                        message="The guest is checked in.")

        # Before the hour: the button is there, greyed out, so the host can see
        # that check-in exists and when it opens — not left wondering.
        if now < opens_at:
            local = timezone.localtime(opens_at)
            return gate(
                "Confirmed", visible=True, state="grey", available=False,
                message=(
                    f"Check-in opens {local.strftime('%d %b %Y')} at "
                    f"{local.strftime('%I:%M %p').lstrip('0')}."
                ),
            )

        if now < grace_ends:
            # The closing stretch of the window turns yellow: the host is still
            # allowed to check the guest in, but it's running out. An hour of
            # warning where the grace period is long enough to spare it, half
            # the window where it isn't.
            warn_after = max(
                self.scheduled_grace_minutes() - GRACE_WARNING_MINUTES,
                self.scheduled_grace_minutes() // 2,
            )
            in_warning = now >= opens_at + timedelta(minutes=warn_after)
            return gate(
                "Check-in window open",
                visible=True,
                state="yellow" if in_warning else "green",
                available=True,
                otp=True,
                message=(
                    "Guest check-in is within the grace period."
                    if in_warning
                    else "Check-in available."
                ),
            )

        # Past the window with nobody in. There is no button here and no
        # decision to offer: `sync_no_show` has cancelled the booking outright
        # by the time anybody reads this, and a host cannot take in a guest on a
        # stay that no longer exists. This branch is what a caller sees in the
        # instant between the window shutting and the booking being read — the
        # answer it gives is the same one it will give afterwards.
        return gate(
            "No Show", visible=False, state="hidden", available=False,
            message=(
                "The guest did not check in before the arrival window closed. "
                "This booking has been cancelled."
            ),
        )

    # --- Leaving early ---
    #
    # A guest may walk out whenever they like, and plenty do — a day early, half
    # a day early. Two things follow from that, and they are deliberately kept
    # apart: the nights they no longer occupy go back on the calendar for other
    # guests, and the money does not move at all. The stay was paid for in full,
    # and leaving early is the guest's decision, not a shortened booking.

    def departure_release(self, now=None):
        """
        What checking out right now would hand back: `(ends_on, nights)`.

        `ends_on` is the date the part in front of us would end on — the guest
        slept every night up to it, so it is the first night that goes back on
        sale. The arrival night is never released: a guest who books a night,
        turns up and leaves again that evening has still used it.

        `nights` is 0 for the ordinary departure on the booked day, which is
        what makes "did they leave early?" a question this one method answers.
        """
        now = now or timezone.now()
        part = self.current_part(now)
        if part is None:
            return self.check_out, 0
        today = timezone.localtime(now).date()
        ends_on = max(today, part["check_in"] + timedelta(days=1))
        if ends_on >= part["check_out"]:
            return part["check_out"], 0
        return ends_on, (part["check_out"] - ends_on).days

    def shorten_part(self, index: int, ends_on) -> None:
        """
        End one part of the stay on `ends_on`, in place. Caller saves.

        Writes the runs out explicitly, which is what actually frees the nights:
        occupancy is read from `segments` everywhere (see `stay_segments`), so a
        run that stops earlier is a run that stops holding those nights.

        `check_in`/`check_out` are deliberately NOT touched. They are the outer
        bounds of what was BOOKED — frozen like the money and the villa snapshot
        beside them — and every reader that cares which nights are actually held
        goes through the segments. The gap between the two is the record of the
        guest having left early, and it is the truth on both counts.
        """
        runs = self.stay_segments()
        if not (1 <= index <= len(runs)):
            return
        start, _ = runs[index - 1]
        if ends_on <= start:
            return
        runs[index - 1] = (start, ends_on)
        self.segments = [
            {"check_in": a.isoformat(), "check_out": b.isoformat()} for a, b in runs
        ]

    def check_out_gate(self, now=None) -> "CheckOutGate":
        """
        Whether the host may check this guest out right now, and what leaving at
        this moment means. See CheckOutGate.
        """
        now = now or timezone.now()
        ends_on, released = self.departure_release(now)

        def gate(available, message, early=False, nights=0):
            return CheckOutGate(
                available=available,
                message=message,
                early=early,
                released_nights=nights,
                ends_on=ends_on,
            )

        if self.status == self.STATUS_CANCELLED:
            return gate(False, "This booking was cancelled.")
        part = self.current_part(now)
        if part is None:
            if self.any_part_arrived:
                return gate(False, "This guest is already checked out.")
            return gate(False, "The guest never checked in — there is nothing to close.")
        if part["checked_in_at"] is None:
            return gate(False, "Check the guest in first.")

        if released:
            nights = f"{released} night{'' if released == 1 else 's'}"
            return gate(
                True,
                (
                    f"This is {nights} before the booked check-out. Those nights go "
                    "back on the calendar for other guests, and no refund is due — "
                    "the stay was paid for in full."
                ),
                early=True,
                nights=released,
            )
        # Past the hour, the PIN stops being the point. It was only ever there
        # to stop a host putting a guest out early, and that can no longer
        # happen — so the host can close this in one press, and what they need
        # to be told is what happens if they don't.
        if self.check_out_overdue(now):
            due_at = self.auto_check_out_at(now)
            when = timezone.localtime(due_at).strftime("%I:%M %p").lstrip("0").lower()
            return gate(
                True,
                (
                    "The booked check-out time has passed, so no PIN is needed — "
                    f"you can close this stay now. Left alone it closes itself at {when}."
                ),
            )
        return gate(
            True,
            "Ask the guest for the 4-digit PIN on their booking to check them out.",
        )

    def hours_late(self, now=None):
        """How many hours past the scheduled check-in the guest still isn't
        checked in. 0 unless the booking is awaiting check-in."""
        now = now or timezone.now()
        if self.lifecycle_status(now) != self.LIFECYCLE_AWAITING:
            return 0.0
        secs = (now - self.check_in_datetime()).total_seconds()
        return max(0.0, secs / 3600.0)

    def cancellation_policy(self, now=None) -> CancellationPolicy:
        """
        What cancelling this booking at `now` would mean — whether it's still
        allowed, how much comes back, and the line to show the guest.

        Everything cancellation-related goes through here: the API fields, the
        confirmation dialog's copy and the fine the mutation freezes onto the
        row. See REFUND_TIERS above for the rule.

        The comparison is datetime against datetime, both timezone-aware:
        `now` is an instant (UTC in the database) and `check_in_datetime()` is
        the property's wall-clock check-in on the check-in date, made aware in
        the project timezone. Comparing dates alone would call 12:30 PM on the
        arrival day "expired" — hence the tier order below, which settles the
        moment first and only then measures the distance to it.
        """
        now = now or timezone.now()
        checkin_at = self.check_in_datetime()

        # Already settled: a cancelled booking reports what it was actually
        # charged, not what a fresh cancellation would cost.
        if self.status == self.STATUS_CANCELLED:
            total = Decimal(str(self.total or 0))
            fee = Decimal(str(self.cancellation_fee or 0))
            pct = int((fee * 100 / total).to_integral_value(ROUND_HALF_UP)) if total else 0
            return CancellationPolicy(
                can_cancel=False,
                refund_percentage=100 - pct,
                penalty_percentage=pct,
                message=self.MSG_ALREADY_CANCELLED,
                refund_amount=total - fee,
                penalty_amount=fee,
            )

        # The stay is under way — the host has the guest on the property.
        if self.checked_in_at is not None:
            return CancellationPolicy.build(
                self.total,
                can_cancel=False,
                refund_percentage=self.REFUND_AFTER_CHECK_IN,
                message=self.MSG_CHECKED_IN,
            )

        refund_percentage, message = self.refund_tier_at(checkin_at, now)
        return CancellationPolicy.build(
            self.total,
            can_cancel=refund_percentage is not None,
            refund_percentage=refund_percentage or self.REFUND_AFTER_CHECK_IN,
            message=message,
        )

    def refund_tier_at(self, arrives_at, now):
        """
        The tier an arrival at `arrives_at` falls into when judged at `now`:
        `(refund_percentage, message)`, with a percentage of None meaning the
        window is shut and nothing may be cancelled any more.

        The one place the sliding scale is actually applied. The whole-stay
        policy asks it about the booking's own check-in; a partial cancellation
        asks it about the arrival of each PART the given-up nights belong to —
        on a split stay the guest arrives more than once, and each arrival has
        its own deadline running against it.

        Distances are measured between two instants, not by calendar day: "24
        hours before 2 PM" is 2 PM the day before, wherever midnight falls.
        """
        # Past the arrival moment the window is shut — there is nothing left to
        # cancel, only a stay to not turn up for.
        if now >= arrives_at:
            return None, self.MSG_EXPIRED

        for hours, percentage, message in self.REFUND_TIERS:
            if now <= arrives_at - timedelta(hours=hours):
                return percentage, message
        # The last tier has a threshold of 0 hours, so the loop always settles;
        # this only guards a table edited down to nothing.
        return self.REFUND_AFTER_CHECK_IN, self.MSG_NO_REFUND

    # --- Giving up part of a stay ---
    #
    # A guest whose plans shrink should not have to throw the whole booking away
    # to drop two nights of it. They pick the nights they no longer want; those
    # nights are priced out of the stay, refunded under the same clock rule as a
    # whole cancellation, and handed straight back to the villa's calendar.
    #
    # Any night that hasn't started may go, on its own, wherever it sits. One
    # taken out of the middle does mean packing up, leaving and coming back —
    # so the stay splits in two there, which is the shape a stay booked around
    # another guest's nights already has and is lived exactly the same way: check
    # out before the gap, check back in after it on a fresh PIN.
    #
    # A stay ALREADY UNDER WAY is trimmed the same way. The night the guest is
    # sleeping in is theirs and is gone, but the nights after it haven't happened
    # yet: they can still be handed back, and handing them back is worth doing
    # even though no money comes with it — the villa gets the dates, the guest
    # gets to leave early on the books rather than in silence. That is the same
    # bargain an early check-out strikes (nights freed, nothing refunded), so a
    # begun part prices at 0% rather than disappearing from the picker.
    #
    # And it goes on pricing at 0% after a split. The ladder is judged at a
    # part's ARRIVAL, so a run broken off a begun part would otherwise open in
    # the future and read as refundable — a guest could hand back one night at
    # 0% and buy back the ladder on all the nights behind it. `under_way` is the
    # mark that says a run came off a part the guest had already begun, and it
    # keeps that run non-refundable for as long as it exists.

    MSG_NIGHT_STARTED = (
        "That night has already begun — it's yours, and it can't be given back. "
        "The nights that haven't started yet still can."
    )
    MSG_STAY_UNDER_WAY = (
        "Your stay has already started, so nothing is refunded for these "
        "nights — they simply go back on the villa's calendar."
    )
    MSG_ARRIVAL_PASSED = (
        "Check-in for this part of the stay has already passed, so nothing is "
        "refunded for these nights — they simply go back on the calendar."
    )
    MSG_NOT_BOOKED = "Those nights aren't part of this booking."
    MSG_NO_NIGHTS = "Choose at least one night to cancel."

    def billed_nights(self) -> int:
        """How many nights the frozen total was priced over. The denominator
        every per-night figure is worked out against, so it stays the BOOKED
        count even after some of those nights have been given up."""
        booked = int(self.nights or 0)
        if booked > 0:
            return booked
        return len(self.occupied_nights()) or 1

    def cancelled_nights(self) -> list:
        """
        Every night given up on this booking and NOT since taken back, in date
        order.

        The receipts are a history, not a state: a guest who dropped a night and
        later bought it again (see properties/additions.py) has a row saying it
        went and a stay that plainly holds it. What is true NOW is the stay, so
        a night the booking currently occupies is not a cancelled night —
        otherwise it would sit greyed out and unreachable on the cancel screen,
        having been paid for twice over.

        The money is untouched by this: `cancelled_value` and `refunded_total`
        read the rows, because what was refunded that day was refunded, whatever
        happened afterwards.
        """
        held = self.occupied_nights()
        nights = set()
        for row in self.cancellations.all():
            for raw in row.nights or []:
                try:
                    night = date.fromisoformat(str(raw)[:10])
                except ValueError:
                    continue
                if night not in held:
                    nights.add(night)
        return sorted(nights)

    def cancelled_value(self) -> Decimal:
        """What the nights already given up were worth of the total. Kept exact
        so a run of partial cancellations can never refund more than the stay
        cost — the last one is handed the remainder rather than its own share."""
        return sum(
            (Decimal(str(row.stay_value or 0)) for row in self.cancellations.all()),
            Decimal("0"),
        )

    def billed_night_set(self) -> list:
        """
        Every night this booking was priced over — the ones it still holds and
        the ones it has since given up — in date order.

        The denominator's dates, where `billed_nights` is only its size. What a
        service covers is worked out against this rather than against what is
        left, so giving up a night doesn't quietly shuffle a service onto a
        different one.
        """
        nights = set(self.occupied_nights())
        for row in self.cancellations.all():
            nights.update(row.night_dates())
        return sorted(nights)

    def service_billed_night_set(self, entry) -> set:
        """
        The nights one extra service was CHARGED over — named, not counted.

        Named, because a count cannot be turned back into the right dates. A
        service is charged over the nights of the stay that hadn't started when
        it was bought (see additions.quote_services), and those nights need not
        sit in a row: a split stay has gaps in it, and so does a stay with
        nights already given up. `dates` on the entry is that exact set, frozen
        at the moment the money was taken, so what a removal hands back is what
        the purchase took — which is the whole point (see properties/removals).

        Everything below it is for entries frozen before `dates` existed, which
        carry only a start and a count. Their nights are re-derived: the booked
        nights from the start onwards, as many of them as the service was billed
        for. That reading is right whenever those nights ran together, and wrong
        the moment they didn't — it reaches across nights the guest had ALREADY
        given up before the service was even bought, spending the service's
        count on dates it was never charged for, and the removal then hands back
        fewer nights than the purchase took. (A night given up AFTER the service
        was bought is the opposite case and must stay counted: it WAS covered,
        and its share came back with the night itself — see `extras_for_nights`.)

        So the early-cancelled nights are dropped — but only while the stricter
        reading can still account for every night the guest was billed for. If
        it can't, the record is too thin to say which nights those were, and the
        old reading stands: under-counting here would be the very bug this is
        fixing, and a guess that refunds too little is worse than the reading
        that at least adds up.
        """
        entry = entry or {}
        stored = set()
        for raw in entry.get("dates") or []:
            try:
                stored.add(date.fromisoformat(str(raw)[:10]))
            except (TypeError, ValueError):
                continue
        if stored:
            return stored

        nights = self.billed_night_set()
        raw = entry.get("added_from")
        if raw:
            try:
                first = date.fromisoformat(str(raw)[:10])
                nights = [n for n in nights if n >= first]
            except ValueError:
                pass

        billed = self.service_nights(entry)
        stamp = parse_datetime(str(entry.get("added_at") or ""))
        bought_at = self._aware(stamp) if stamp else None
        if bought_at:
            gone_first = set()
            for row in self.cancellations.all():
                if row.created_at and row.created_at < bought_at:
                    gone_first.update(row.night_dates())
            # A night given up and later bought back is on the stay again, so it
            # was never missing from what the service could be charged over.
            gone_first -= set(self.occupied_nights())
            strict = [n for n in nights if n not in gone_first]
            if len(strict) >= billed:
                nights = strict
        return set(nights[:billed])

    def service_covered_nights(self, entry) -> set:
        """
        Which nights one extra service actually covers — NOW.

        What it was charged over (see `service_billed_night_set`), less what it
        has since STOPPED covering. A service the guest has given back covers
        only the nights it was actually delivered on: the ones it stopped
        covering are named on the entry (see `service_refunded_night_set`) and
        taken out here, so cancelling one of them later can't hand the same
        money back a second time.
        """
        return self.service_billed_night_set(entry) - self.service_refunded_night_set(
            entry
        )

    def extras_for_night(self, night) -> Decimal:
        """What the extra services on this booking cost for ONE given night."""
        total = Decimal("0.00")
        for entry in self.service_entries():
            if night in self.service_covered_nights(entry):
                total += Decimal(str(entry.get("price", 0) or 0))
        return total

    def extras_for_nights(self, nights) -> Decimal:
        """
        What the extra services on these nights cost, all together.

        This is the part of a cancellation that comes back IN FULL: a service
        is something the host was going to do on a night that is no longer
        happening, so there is nothing for the ladder to keep a percentage of.
        The accommodation is what the sliding scale is about.
        """
        return sum(
            (self.extras_for_night(n) for n in nights), Decimal("0.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def service_fee_for_nights(self, nights) -> Decimal:
        """
        The platform's own fee on these nights — the part of their worth that
        NEVER comes back.

        The fee buys the booking, not the stay: the platform did its work when
        the villa was found, held and paid for, and it did that work whether or
        not the guest turns up. So it sits outside the ladder in the other
        direction from the extra services, which come back whole because the
        host has not delivered them yet.

        Flat per night, like the accommodation it was charged on, so a night's
        share of it is checkable against the receipt.
        """
        count = len(list(nights))
        if not count:
            return Decimal("0.00")
        return (
            Decimal(str(self.service_fee or 0)) * Decimal(count)
            / Decimal(self.billed_nights())
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def stay_only_value(self, count: int) -> Decimal:
        """
        What `count` nights of ACCOMMODATION are worth — the stay's own money
        (rate, fee, tax, less any discount) without the extra services on top.

        Flat across the booking, which is what makes it checkable: five nights
        of an eight-night stay is five-eighths of it. The services are added per
        night by `extras_for_nights`, because they are not flat.
        """
        stay_only = Decimal(str(self.total or 0)) - Decimal(str(self.extras_total or 0))
        return (stay_only * Decimal(count) / Decimal(self.billed_nights())).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def nights_value(self, nights, *, empties: bool) -> Decimal:
        """
        What these nights are worth: their share of what was actually paid.

        The stay itself is flat — one nightly rate, with the fee, tax and
        discount around it belonging to the whole booking — so every night
        carries the same share of it. That keeps the arithmetic something a
        guest can check: five nights of an eight-night booking is five-eighths
        of the accommodation.

        The EXTRA SERVICES are not flat, and are added per night on top. A
        service bought halfway through a stay covers only the nights that were
        left (see `service_covered_nights`), so averaging it across the whole
        booking would refund it on nights it was never delivered on and
        short-change the nights it was. Giving up a night hands back that
        night's services with it — at whatever the cancellation policy allows,
        like the rest of the money.

        `empties` is what closes the rounding: the cancellation that gives up
        the last of the stay is worth everything not already given up, so a
        third of a rupee lost to rounding three times over cannot leave a
        fully-cancelled booking a rupee short.
        """
        total = Decimal(str(self.total or 0))
        already = self.cancelled_value()
        if empties:
            return max(Decimal("0"), total - already)
        chosen = list(nights)
        stay_only = total - Decimal(str(self.extras_total or 0))
        share = (
            stay_only * Decimal(len(chosen)) / Decimal(self.billed_nights())
            + sum((self.extras_for_night(n) for n in chosen), Decimal("0.00"))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return max(Decimal("0"), min(share, total - already))

    def night_starts_at(self, night):
        """When one night of the stay begins: the property's check-in hour on
        that date.

        A night is the guest's from that moment — they can walk in — and until
        it they can still hand it back. It is the check-in hour and not midnight
        because that is when a night becomes unsellable to anybody else, and it
        is per NIGHT rather than per part so that a stay under way can still be
        cut short from tomorrow onward.
        """
        return self._aware(datetime.combine(night, self.scheduled_check_in_time()))

    def cancellable_parts(self, now=None):
        """
        The parts of this stay that still hold nights the guest may give up,
        each with those nights and the tier they'd come back at.

        Two kinds of part appear here, and the difference is `begun`. One
        already under way — the guest is in, or its check-in hour has been and
        gone — keeps its money: the nights that haven't started can still be
        handed back, but at 0%, which is the same bargain leaving early strikes.
        One nobody has arrived for is still on the ladder, and there the tier
        below is only the part's HEADLINE, judged at its arrival: what each of
        its nights actually comes back at is judged on that night's own notice
        (see `night_refund_tiers`, which is what the picker and the quote read).
        A part with nothing left to give up is not listed at all.

        Each entry is the `stay_segment_windows` tuple plus its 1-based index,
        the nights still open, whether it has begun, and that headline tier.
        """
        now = now or timezone.now()
        stays = self.part_stays()
        out = []
        for index, (start, end, opens_at, ends_at) in enumerate(
            self.stay_segment_windows(), start=1
        ):
            stay = stays.get(index, {})
            arrived_at = stay.get("checked_in_at")
            # A part the guest has already left is finished with: what remains of
            # it in `segments` is the past, and an early departure has handed the
            # unused nights back already. Nothing here to give up.
            if stay.get("checked_out_at"):
                continue
            arrived = bool(arrived_at)
            # `under_way`: this run was broken off a part the guest had already
            # begun, so its money is spent even though its own arrival is still
            # ahead — see the note above MSG_NIGHT_STARTED.
            carried = bool(stay.get("under_way"))
            begun = arrived or carried or now >= opens_at
            if begun:
                percentage = self.REFUND_AFTER_CHECK_IN
                message = (
                    self.MSG_STAY_UNDER_WAY
                    if arrived or carried
                    else self.MSG_ARRIVAL_PASSED
                )
            else:
                # Not begun, so the arrival is still ahead and the ladder always
                # settles on a band — `None` is unreachable here.
                percentage, message = self.refund_tier_at(opens_at, now)
                if percentage is None:
                    continue
            # The nights of this part that haven't started yet. On a part still
            # ahead of the guest that is all of them; on one under way it is
            # everything after tonight — the night being slept in is theirs.
            #
            # A guest let in ahead of the hour is IN, so their first night starts
            # when they arrived rather than when the clock said it would; every
            # night after that runs on the ordinary hour.
            nights, night, first = [], start, True
            while night < end:
                begins = self.night_starts_at(night)
                if first and arrived:
                    begins = min(begins, arrived_at)
                if now < begins:
                    nights.append(night)
                night += timedelta(days=1)
                first = False
            if not nights:
                continue
            out.append(
                {
                    "index": index,
                    "check_in": start,
                    "check_out": end,
                    "opens_at": opens_at,
                    "ends_at": ends_at,
                    "nights": nights,
                    "begun": begun,
                    "refund_percentage": percentage,
                    "message": message,
                }
            )
        return out

    def night_refund_tiers(self, now=None) -> dict:
        """
        Every night the guest may still give up, mapped to the tier THAT NIGHT
        comes back at: `{night: (refund_percentage, message)}`.

        The one place the per-night ladder is applied, and the authority for
        every figure the guest is shown: the chips on the picker, the quote
        underneath them and the cancellation itself all read this, so a night
        labelled "90% back" is refunded at 90% when it actually goes.

        Each night is judged on ITS OWN notice — the distance from now to the
        hour that night begins — not on the stay's arrival. A fortnight's stay
        starting on Friday is a fortnight of different deadlines, and pricing
        the last night off the first would charge a guest for lateness they were
        nowhere near: the night they are handing back is three weeks away.

        A stay already under way is NO exception. Checking in settles the night
        the guest is standing in — that one is theirs and is not on this list at
        all — but it says nothing about a night ten days off. The host has the
        same notice on that night as they would have had if the guest had never
        arrived, so it comes back on the same band. A guest who checks in on
        Friday and finds out on Saturday that they must leave next week is not
        thereby charged for a fortnight nobody will sleep in.
        """
        now = now or timezone.now()
        tiers = {}
        for part in self.cancellable_parts(now):
            for night in part["nights"]:
                percentage, message = self.refund_tier_at(
                    self.night_starts_at(night), now
                )
                # Unreachable: a night listed here has not begun, so its moment
                # is still ahead and the ladder always settles on a band.
                if percentage is None:
                    percentage, message = self.REFUND_AFTER_CHECK_IN, self.MSG_NO_REFUND
                tiers[night] = (percentage, message)
        return tiers

    def cancellable_nights(self, now=None) -> list:
        """Every night the guest could still choose to give up, in date order.

        Every night that hasn't started, whatever part it sits in. Each stands
        on its own: any of them may go without its neighbours, and what that
        leaves behind — one run or two — is the cancellation's to reshape.
        """
        if self.status == self.STATUS_CANCELLED:
            return []
        nights = []
        for part in self.cancellable_parts(now):
            nights.extend(part["nights"])
        return sorted(nights)

    # --- The stay, night by night ---
    #
    # The picker needs more than the list of nights that MAY go: it has to say,
    # on each night itself and before anything is chosen, what giving that night
    # up would hand back — or, when it can't go, why not. Both answers come from
    # the same clock rule the quote and the cancellation use (refund_tier_at),
    # so a night labelled "90% back" is priced at 90% when it actually goes.
    #
    # The per-night money is INDICATIVE: one night's pro-rata share, refunded at
    # its own part's tier. What the guest is charged is always the quote for the
    # whole selection, which settles the rounding across the nights picked — so
    # the chips guide the choice and the summary underneath is the price.

    NIGHT_OPEN = "open"            # hasn't started — may still be given up
    NIGHT_STARTED = "started"      # the guest is in it; it's theirs
    NIGHT_EXPIRED = "expired"      # it has begun with nobody arrived for it
    NIGHT_CANCELLED = "cancelled"  # already given up

    # Why a night can't go. `MSG_NIGHT_STARTED` doubles as the refusal the quote
    # returns (see above), so a guest who somehow submits a spent night is told
    # the same thing the chip already said.
    MSG_NIGHT_EXPIRED = (
        "This night has already begun, so it can no longer be cancelled. The "
        "nights that haven't started yet still can."
    )
    MSG_NIGHT_CANCELLED = "This night has already been cancelled."

    def _part_index_for(self, night) -> int:
        """Which part of the stay a night belongs to, 1-based.

        Nights already given up are gone from `segments` — that IS how they were
        handed back — so they have no part of their own any more. They are
        placed where they were: inside the part that still covers them, or
        against the part they were trimmed off the back of (the one before them),
        falling back to the one they were trimmed off the front of.
        """
        runs = self.stay_segments()
        previous = 0
        for index, (start, end) in enumerate(runs, start=1):
            if start <= night < end:
                return index
            if night < start:
                # Trimmed off the tail of the part before it, or — with nothing
                # before it — off the head of this one.
                return previous or index
            previous = index
        return previous or 1

    def night_options(self, now=None) -> list:
        """
        Every night this booking was booked for — the ones still held AND the
        ones already given up — in date order, each with what cancelling it at
        `now` would mean.

        This is what the guest's cancel screen is drawn from: the whole stay is
        laid out, each night carrying its own state, its own refund tier and the
        sentence explaining it, so a night that cannot go says so where it is
        rather than only after it's been picked.
        """
        now = now or timezone.now()

        # What each already-cancelled night actually got back, off the receipt
        # it went out on — the event's own figures split across its nights, so
        # a night cancelled at 90% never reads as anything else later.
        # A night the booking holds again is not a cancelled night, however many
        # receipts say it once was — it was bought back, and the picker has to
        # offer it like any other night of the stay (see `cancelled_nights`).
        held = self.occupied_nights()
        settled = {}
        for row in self.cancellations.all():
            went = row.night_dates()
            if not went:
                continue
            # Divided across every night the EVENT covered, not just the ones
            # still gone: what one night got back that day doesn't change
            # because a different night was later bought again.
            count = Decimal(len(went))
            dates = [n for n in went if n not in held]
            if not dates:
                continue
            value = (Decimal(str(row.stay_value or 0)) / count).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            refund = (Decimal(str(row.refund_amount or 0)) / count).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            for night in dates:
                settled[night] = {
                    "stay_value": value,
                    "refund_amount": refund,
                    "refund_percentage": int(row.refund_percentage or 0),
                    "message": row.message or self.MSG_NIGHT_CANCELLED,
                }

        rows = []

        def add(night, index, state, percentage, message, value, refund):
            rows.append(
                {
                    "date": night,
                    "part_index": index,
                    "state": state,
                    "cancellable": state == self.NIGHT_OPEN,
                    "refund_percentage": percentage,
                    "stay_value": value,
                    "refund_amount": refund,
                    "cancellation_fee": value - refund,
                    "message": message,
                }
            )

        # Nights the booking still holds, judged ONE BY ONE: a night that hasn't
        # started is open at its part's tier — which is 0% once that part is
        # under way — and a night that has started is the guest's and is shut.
        # So a stay in its second of three nights offers the third, at no refund,
        # instead of vanishing from the picker along with the night being slept.
        open_nights = self.night_refund_tiers(now)
        stays = self.part_stays()
        for index, (start, end, _opens_at, _ends_at) in enumerate(
            self.stay_segment_windows(), start=1
        ):
            arrived = bool(
                stays.get(index, {}).get("checked_in_at")
                or stays.get(index, {}).get("checked_out_at")
            )
            night = start
            while night < end:
                if night in settled:
                    night += timedelta(days=1)
                    continue
                tier = open_nights.get(night)
                if tier is not None:
                    state = self.NIGHT_OPEN
                    percentage, message = tier
                elif arrived:
                    state, percentage, message = (
                        self.NIGHT_STARTED, 0, self.MSG_NIGHT_STARTED,
                    )
                else:
                    state, percentage, message = (
                        self.NIGHT_EXPIRED, 0, self.MSG_NIGHT_EXPIRED,
                    )
                # A booking cancelled before receipts were kept has nothing in
                # `settled` to place its nights with; they are still gone, and
                # the picker must not offer them.
                if self.status == self.STATUS_CANCELLED:
                    state, percentage, message = (
                        self.NIGHT_CANCELLED,
                        0,
                        self.MSG_ALREADY_CANCELLED,
                    )
                # THIS night's own worth, not the stay's average: a night the
                # guest bought breakfast for is worth more than one they didn't,
                # and the chip has to say so before it is picked (see
                # nights_value, which prices the selection the same way).
                #
                # And the split the ladder is charged against: the services come
                # back whole, only the accommodation is tiered, so a night with
                # a service on it hands back more than its percentage suggests.
                value = self.nights_value([night], empties=False)
                extras = min(self.extras_for_night(night), value)
                # The platform's fee on this night is kept whatever the band
                # says, so the ladder is charged against what is left after it
                # and after the services (see `service_fee_for_nights`).
                fee = min(self.service_fee_for_nights([night]), value - extras)
                refund = (
                    extras
                    + (
                        (value - extras - fee) * Decimal(percentage) / Decimal(100)
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    # A night that can't go hands back nothing — not even its
                    # services. It is being slept in, or its hour went by with
                    # the host standing ready: either way the evening was
                    # delivered. Only an OPEN night has a refund to quote.
                    if state == self.NIGHT_OPEN
                    else Decimal("0.00")
                )
                add(night, index, state, percentage, message, value, refund)
                night += timedelta(days=1)

        # And the nights already given up, back in their place in the stay. A
        # cancelled booking's nights all land here — its `segments` are left
        # standing as the record of what was held (see apply_nights_cancellation).
        for night, done in settled.items():
            add(
                night,
                self._part_index_for(night),
                self.NIGHT_CANCELLED,
                done["refund_percentage"],
                done["message"],
                done["stay_value"],
                done["refund_amount"],
            )

        rows.sort(key=lambda row: row["date"])
        return rows

    def nights_cancellation_quote(self, nights, now=None) -> NightsCancellationQuote:
        """
        Price and check a set of nights the guest wants to give up.

        Refused, with `error` saying why, when the selection isn't this
        booking's to give up or has already begun. Nights are chosen one at a
        time and need not sit together: handing back one out of the middle
        simply breaks that part into the two runs either side of it, the same
        shape a stay booked around another guest's nights already has.
        Otherwise every chosen night is priced at its share of the total and
        refunded at ITS OWN tier — the ladder judged on the distance to that
        night, not to the stay's arrival (see `night_refund_tiers`). A fortnight
        booked from Friday is a fortnight of different deadlines, and this adds
        them up rather than pretending one rule covers all of them.
        """
        now = now or timezone.now()
        chosen = sorted({n for n in nights})

        def refuse(error):
            return NightsCancellationQuote(
                nights=tuple(chosen),
                stay_value=Decimal("0.00"),
                penalty_amount=Decimal("0.00"),
                refund_amount=Decimal("0.00"),
                refund_percentage=0,
                full=False,
                allowed=False,
                message=error,
                error=error,
            )

        if self.status == self.STATUS_CANCELLED:
            return refuse(self.MSG_ALREADY_CANCELLED)
        if not chosen:
            return refuse(self.MSG_NO_NIGHTS)

        held = self.occupied_nights()
        if not set(chosen) <= held:
            return refuse(self.MSG_NOT_BOOKED)

        # What each chosen night comes back at — and, first, whether it is still
        # the guest's to give back at all. A night that has begun is spent,
        # whichever part it belongs to; the rest of that part is not.
        tiers = self.night_refund_tiers(now)
        for night in chosen:
            if night not in tiers:
                return refuse(self.MSG_NIGHT_STARTED)

        # A night out of the middle is allowed, and needs nothing said here:
        # `drop_nights` rewrites the part as the two runs either side of it, and
        # two runs with somebody else's nights between them is exactly what a
        # split stay already is — lived in instalments, each part checked into
        # on its own PIN. So the guest may hand back any night they haven't
        # started, one at a time, and the stay reshapes around the choice.

        empties = set(chosen) >= held
        stay_value = self.nights_value(chosen, empties=empties)
        # What those nights are worth splits three ways, and only one of the
        # three is the ladder's business.
        #
        #   * the extra services on them come back WHOLE — the host has not
        #     delivered them (see `extras_for_nights`);
        #   * the platform's fee on them comes back NOT AT ALL — it bought the
        #     booking, and the booking happened (see `service_fee_for_nights`);
        #   * what is left is the accommodation and its tax, and that is what
        #     the sliding scale is charged against, night by night.
        extras_value = min(self.extras_for_nights(chosen), stay_value)
        fee_kept = min(
            self.service_fee_for_nights(chosen), stay_value - extras_value
        )
        accommodation = stay_value - extras_value - fee_kept

        # Priced NIGHT BY NIGHT, then added up: each night carries its own tier,
        # because each was given its own notice (see `night_refund_tiers`). The
        # accommodation is flat per night — the rate, fee, tax and discount all
        # belong to the whole stay — so every night takes an equal share of it,
        # and the last takes the remainder, which is what keeps the pieces
        # summing to `accommodation` to the rupee.
        refund = extras_value
        spent = Decimal("0.00")
        count = len(chosen)
        # How many nights came back at each percentage, in the order the ladder
        # gives them — what the summary line is written from below.
        bands = {}
        for position, night in enumerate(chosen, start=1):
            if position == count:
                value = accommodation - spent
            else:
                value = (accommodation / Decimal(count)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            spent += value
            percentage, message = tiers[night]
            refund += (value * Decimal(percentage) / Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            band = bands.setdefault(percentage, {"nights": 0, "message": message})
            band["nights"] += 1

        refund = max(Decimal("0.00"), min(refund, stay_value))
        percentage = (
            int((refund * 100 / stay_value).to_integral_value(ROUND_HALF_UP))
            if stay_value
            else 0
        )
        return NightsCancellationQuote(
            nights=tuple(chosen),
            stay_value=stay_value,
            penalty_amount=stay_value - refund,
            refund_amount=refund,
            refund_percentage=percentage,
            full=empties,
            allowed=True,
            message=self._refund_summary(bands),
            extras_value=extras_value,
            service_fee=fee_kept,
        )

    @staticmethod
    def _refund_summary(bands: dict) -> str:
        """The sentence under a selection priced across more than one tier.

        One band speaks for itself — it is the ladder's own line, the same one
        the night's chip carries. Several cannot: stacking three tier sentences
        would read as three contradictory rules rather than as one selection
        with different notice on each night. So they are counted instead, in
        ladder order, and the guest is told what they can check against the
        chips they just tapped."""
        if not bands:
            return Booking.MSG_FREE
        if len(bands) == 1:
            return next(iter(bands.values()))["message"]
        parts = [
            f"{percentage}% on {band['nights']} night"
            f"{'' if band['nights'] == 1 else 's'}"
            for percentage, band in sorted(bands.items(), reverse=True)
        ]
        return (
            "Each night is refunded on its own notice: "
            + ", ".join(parts[:-1])
            + f" and {parts[-1]}."
        )

    def drop_nights(self, nights, now=None) -> None:
        """
        Take `nights` out of the stay, in place. Caller saves.

        Occupancy is read from `segments` everywhere (see `stay_segments`), so
        rewriting the runs without those nights IS what hands them back to the
        calendar — the same mechanism an early check-out uses.

        `check_in`/`check_out` are left alone on purpose: they are the outer
        bounds of what was BOOKED, frozen beside the money, and the gap between
        them and the segments is the record of what was given up.

        A part can come out of this as more than one run: a night taken from its
        middle leaves the nights either side of it, and those are two runs now.
        Per-part history is re-keyed onto them — the arrival stamps stay with the
        run the guest actually arrived for, and every run off a part that had
        already begun is marked `under_way`, so the money that was spent on
        those nights stays spent however late their new arrival looks.
        """
        now = now or timezone.now()
        drop = set(nights)
        old_runs = self.stay_segments()
        stays = self.part_stays()
        # Every new run each old part broke into, in order — the first is the one
        # the part's own history belongs to.
        runs, born = [], {}
        for index, (start, end) in enumerate(old_runs, start=1):
            night, run_start = start, None
            while night < end:
                if night in drop:
                    if run_start is not None:
                        runs.append((run_start, night))
                        born.setdefault(index, []).append(len(runs))
                        run_start = None
                elif run_start is None:
                    run_start = night
                night += timedelta(days=1)
            if run_start is not None:
                runs.append((run_start, end))
                born.setdefault(index, []).append(len(runs))
        self.segments = [
            {"check_in": a.isoformat(), "check_out": b.isoformat()} for a, b in runs
        ]

        rows = []
        for index, (start, _end) in enumerate(old_runs, start=1):
            new_indexes = born.get(index)
            if not new_indexes:
                # The whole part went. Its history goes with it rather than
                # being inherited by a part that isn't it.
                continue
            stay = stays.get(index, {})
            # Had this part begun? Then so has every run left of it — including
            # the first, whose start may itself have moved later.
            begun = bool(
                stay.get("checked_in_at")
                or stay.get("under_way")
                or now >= self._aware(
                    datetime.combine(start, self.scheduled_check_in_time())
                )
            )
            head = {"index": new_indexes[0]}
            for key in ("checked_in_at", "checked_out_at"):
                if stay.get(key):
                    head[key] = stay[key].isoformat()
            if stay.get("guests"):
                head["guests"] = stay["guests"]
            if begun:
                head["under_way"] = True
            if len(head) > 1:
                rows.append(head)
            # The runs behind the gap: nobody has arrived for them and nobody
            # has left them, but they are what remains of a stay under way.
            for tail in new_indexes[1:]:
                if begun:
                    rows.append({"index": tail, "under_way": True})
        self.segment_stays = rows

    def apply_nights_cancellation(self, quote, now=None):
        """
        Carry out the cancellation `quote` describes and return its record.

        Both kinds land here — a whole stay called off is the same act as
        giving up every night still held — so the receipt, the running fee and
        the freed calendar are written the same way whichever door it came in
        by. Call inside a transaction; the caller saves nothing else.
        """
        if not quote.allowed:
            raise ValueError(quote.error or self.MSG_NO_NIGHTS)
        now = now or timezone.now()
        # A booking fetched with `prefetch_related("cancellations")` is holding a
        # cached list of the rows that existed when it was read. Adding to that
        # relation does not refresh it, so anything asked afterwards — the
        # response this mutation returns, the value of a second cancellation in
        # the same request — would be answered from the state BEFORE this one.
        # Drop the cache and let the next read go to the database.
        getattr(self, "_prefetched_objects_cache", {}).pop("cancellations", None)
        record = BookingCancellation.objects.create(
            booking=self,
            kind=(
                BookingCancellation.KIND_FULL
                if quote.full
                else BookingCancellation.KIND_PARTIAL
            ),
            nights=[n.isoformat() for n in quote.nights],
            nights_count=quote.nights_count,
            stay_value=quote.stay_value,
            cancellation_fee=quote.penalty_amount,
            refund_amount=quote.refund_amount,
            refund_percentage=quote.refund_percentage,
            extras_refund=quote.extras_value,
            message=quote.message,
        )
        # The booking-level fee is the RUNNING total of every event's penalty,
        # so a stay trimmed twice and then called off reports what the guest was
        # charged altogether, not just the last instalment.
        self.cancellation_fee = (
            Decimal(str(self.cancellation_fee or 0)) + quote.penalty_amount
        )
        fields = ["cancellation_fee", "updated_at"]
        if quote.full:
            self.status = self.STATUS_CANCELLED
            self.cancelled_at = now
            fields += ["status", "cancelled_at"]
            # `segments` deliberately untouched: with the booking cancelled it
            # blocks nothing (occupancy counts active bookings only), and it
            # stays the record of the nights that were held.
        else:
            self.drop_nights(quote.nights, now)
            fields += ["segments", "segment_stays"]
        self.save(update_fields=fields)
        return record

    def refunded_total(self) -> Decimal:
        """Everything handed back across every cancellation on this booking."""
        return sum(
            (Decimal(str(row.refund_amount or 0)) for row in self.cancellations.all()),
            Decimal("0"),
        )

    def can_cancel(self, now=None):
        """Whether the guest may still call this stay off (see the policy)."""
        return self.cancellation_policy(now).can_cancel

    def cancel_fee_at(self, now=None):
        """The penalty a cancellation right now would carry, in currency."""
        return self.cancellation_policy(now).penalty_amount

    # --- Adding to a stay that has already been paid for ---
    #
    # A booking is not finished with the moment it is taken. The guest may want
    # the airport pickup they skipped, or two more nights on the end — and the
    # answer to both is "yes, and here is what it costs", not "cancel and book
    # again". What can be added is worked out here; `properties/additions.py`
    # prices it and carries it out.

    def unstarted_nights(self, now=None) -> list:
        """
        The nights of this stay that haven't begun yet, in date order.

        A night starts at the property's check-in hour (see `night_starts_at`),
        and once it has, it is being lived in: it can't be handed back, and a
        service bought today cannot be delivered on it retrospectively. So this
        is the stretch of the stay any addition is priced over — the same
        stretch a cancellation may still give up.
        """
        now = now or timezone.now()
        return sorted(n for n in self.occupied_nights() if now < self.night_starts_at(n))

    def service_nights(self, entry) -> int:
        """
        How many nights one frozen extra service was charged over.

        Services bought at checkout ran the whole stay and carry no count of
        their own; one bought later runs only from the night it was bought, and
        says so. The fallback is what keeps every booking taken before this
        existed reading exactly as it always did.

        `dates` says the same thing more precisely and is written beside the
        count on every service bought since it existed, so the two can only
        disagree on an entry hand-edited between them — where the named dates
        are the better answer.
        """
        try:
            count = int((entry or {}).get("nights") or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return count
        named = len((entry or {}).get("dates") or [])
        return named if named > 0 else self.billed_nights()

    def service_refunded_night_set(self, entry) -> set:
        """
        The nights one service STOPPED covering when the guest gave it back.

        Named one by one rather than counted, because they are not a tidy tail.
        A stay with a night cancelled out of its middle has a service covering
        dates either side of a gap, and "the last two" would then mean the wrong
        two: what the removal gave back is exactly these dates and no others.

        Empty on every service still running (see properties/removals.py).
        """
        out = set()
        for raw in (entry or {}).get("refunded_nights") or []:
            try:
                out.add(date.fromisoformat(str(raw)[:10]))
            except (TypeError, ValueError):
                continue
        return out

    def service_refunded_value(self, entry) -> Decimal:
        """What was actually handed back off one service when it was dropped.

        Read off the entry rather than multiplied out, because it is NOT simply
        price × nights-no-longer-covered: a night the guest had already
        cancelled stops being covered too, but its money came back with the
        cancellation, not with this."""
        try:
            value = Decimal(str((entry or {}).get("refunded_amount") or 0))
        except (TypeError, ValueError, InvalidOperation):
            return Decimal("0.00")
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def service_live_nights(self, entry) -> int:
        """
        How many nights one service still RUNS for: what it was billed over,
        less the nights it no longer covers.

        The billed count is left alone by a removal, because `total` and
        `extras_total` are, exactly as they are by a night cancellation — they
        are the frozen price of what was bought, and every per-night figure is
        worked out against them. What the refund changes is what the service
        still COVERS, and that is this.
        """
        return max(0, self.service_nights(entry) - len(self.service_refunded_night_set(entry)))

    def service_removed(self, entry) -> bool:
        """Whether this service was given back. A service refunded even in part
        has stopped: only nights that hadn't started could be refunded, so what
        is left of it is behind the guest, never in front of them."""
        return bool(self.service_refunded_night_set(entry))

    def service_entries(self) -> list:
        """The booking's extra services as clean dicts, bad rows dropped.

        EVERY one of them, removed ones included — this is what the booking was
        charged for, and `extras_value` is worked out over it. What the stay
        still has is `live_service_entries`."""
        return [
            s
            for s in (self.extra_services or [])
            if isinstance(s, dict) and str(s.get("name", "")).strip()
        ]

    def live_service_entries(self) -> list:
        """The extra services this stay still HAS — the ones not given back.

        What the guest is actually getting, so it is these that a stay extension
        carries over its new nights, these that are offered back for removal,
        and only these that stop the same service being bought again."""
        return [s for s in self.service_entries() if not self.service_removed(s)]

    def extras_value(self) -> Decimal:
        """
        What the extra services on this booking come to: each one's per-night
        price over the nights it was actually bought for.

        The authority for `extras_total`, recomputed rather than incremented so
        a stay that grew and then had a service added can't drift out of step
        with the lines the guest is shown.
        """
        total = Decimal("0.00")
        for entry in self.service_entries():
            price = Decimal(str(entry.get("price", 0) or 0))
            total += price * Decimal(self.service_nights(entry))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def taken_service_names(self) -> set:
        """The services this booking still HAS, lower-cased for comparison.

        A service the guest gave back is not one of them: they were refunded for
        it and may perfectly well change their mind and buy it again, which is
        the whole reason removing one is not the end of the story."""
        return {
            str(s.get("name", "")).strip().lower() for s in self.live_service_entries()
        }

    def available_extra_services(self) -> list:
        """
        The villa's extra services this booking does NOT already have, as
        [{"name", "price"}] straight off the listing.

        Empty when the guest ticked everything on offer (or the host offers
        nothing) — which is precisely when the "add a service" door should not
        be there at all, rather than opening onto an empty list.
        """
        taken = self.taken_service_names()
        out = []
        for raw in self.villa.extra_services or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name or name.lower() in taken:
                continue
            try:
                price = round(float(raw.get("price", 0) or 0), 2)
            except (TypeError, ValueError):
                price = 0.0
            out.append({"name": name, "price": max(0.0, price)})
        return out

    def add_nights(self, nights) -> None:
        """
        Take `nights` INTO the stay, in place. Caller saves.

        The mirror of `drop_nights`: occupancy is read from `segments`, so
        rewriting the runs WITH these nights is what takes them off the villa's
        calendar. Nights next to an existing run join it, and nights that fill
        the gap in a split stay weld its two parts back into one.

        `check_in`/`check_out` DO move here, unlike on a cancellation — they are
        the outer bounds of the stay, and a stay that now runs two nights longer
        genuinely ends two nights later. They only ever WIDEN: a booking trimmed
        at one end keeps the bounds it was made with, because the gap between
        them and the segments is the record of what was given up (see
        `drop_nights`). Per-part stamps follow their part into
        whichever run it ended up in; two parts welded together keep the earlier
        arrival and the later departure, because that is what happened.
        """
        held = self.occupied_nights() | set(nights)
        if not held:
            return
        stays = self.part_stays()
        old_runs = self.stay_segments()

        runs, night = [], None
        for day in sorted(held):
            if night is not None and day == night:
                runs[-1] = (runs[-1][0], day + timedelta(days=1))
            else:
                runs.append((day, day + timedelta(days=1)))
            night = day + timedelta(days=1)

        # Each old part lands in the run that now contains its first night.
        index_map = {}
        for old_index, (start, _end) in enumerate(old_runs, start=1):
            for new_index, (run_start, run_end) in enumerate(runs, start=1):
                if run_start <= start < run_end:
                    index_map[old_index] = new_index
                    break

        merged = {}
        for old_index, stay in sorted(stays.items()):
            new_index = index_map.get(old_index)
            if new_index is None:
                continue
            row = merged.setdefault(new_index, {})
            for key in ("checked_in_at", "checked_out_at"):
                value = stay.get(key)
                if value is None:
                    continue
                current = row.get(key)
                # Two parts welded into one: arrival is the earlier of theirs,
                # departure the later — the run really was entered once and left
                # once, whatever it used to be split into.
                row[key] = (
                    value
                    if current is None
                    else (min(current, value) if key == "checked_in_at" else max(current, value))
                )
            if stay.get("guests"):
                row["guests"] = max(int(row.get("guests") or 0), int(stay["guests"]))
            # Nights bought back around a run that was under way don't undo that:
            # the run is still what's left of a stay already begun, so it stays
            # non-refundable (see `drop_nights`).
            if stay.get("under_way"):
                row["under_way"] = True

        self.check_in = min(runs[0][0], self.check_in)
        self.check_out = max(runs[-1][1], self.check_out)
        # Left empty only when the two dates above describe the stay exactly —
        # the one case `stay_segments()` falls back to them. A single run that
        # starts late because its first night was given up is NOT that case: the
        # fallback would hand the guest back a night they cancelled.
        self.segments = (
            []
            if runs == [(self.check_in, self.check_out)]
            else [
                {"check_in": a.isoformat(), "check_out": b.isoformat()}
                for a, b in runs
            ]
        )
        if merged:
            self.segment_stays = [
                {
                    "index": index,
                    **{
                        key: (value.isoformat() if hasattr(value, "isoformat") else value)
                        for key, value in row.items()
                    },
                }
                for index, row in sorted(merged.items())
            ]


class BookingAddition(models.Model):
    """
    One thing added to a booking after it was paid for, and the payment that
    covered it.

    A booking can grow more than once — a chef this week, two extra nights next
    — so what was added is a LIST of events beside `BookingCancellation`'s list
    of what was given up. Each row is the frozen receipt for one of them: what
    it was, what it cost, and how it was paid for, in the words the guest
    confirmed. The booking's own money columns carry the running totals, so
    everything that only cared "what does this stay cost" is untouched.
    """

    KIND_SERVICES = "services"
    KIND_NIGHTS = "nights"
    # Both in one go — one decision, one payment (see additions.apply_changes).
    KIND_BOTH = "both"
    KIND_CHOICES = [
        (KIND_SERVICES, "Extra services"),
        (KIND_NIGHTS, "Extra nights"),
        (KIND_BOTH, "Nights and services"),
    ]

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="additions"
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_SERVICES)
    # The services bought in this event, as [{"name", "price", "nights",
    # "amount"}]. On a nights purchase these are the services already on the
    # booking, charged on for the new nights — the guest keeps what they had.
    services = models.JSONField(default=list, blank=True)
    # The nights added, as ISO dates in order (empty on a services purchase).
    nights = models.JSONField(default=list, blank=True)
    nights_count = models.PositiveIntegerField(default=0)
    # The split of what was charged. `amount` = accommodation + service_fee +
    # tax + extras, always — the same arithmetic the original checkout ran, on
    # what was added. No discount or coupon applies: those were this stay's, at
    # the moment it was booked.
    accommodation = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    extras = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # How this addition was paid for — masked exactly like the booking's own.
    payment_method = models.CharField(max_length=60, blank=True)
    payment_reference = models.CharField(max_length=24, blank=True)
    # The line the guest read as they confirmed it.
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_bookingaddition"
        ordering = ["created_at"]

    def __str__(self):
        return f"Addition #{self.pk} — booking {self.booking_id} ({self.kind})"

    def night_dates(self) -> list:
        out = []
        for raw in self.nights or []:
            try:
                out.append(date.fromisoformat(str(raw)[:10]))
            except ValueError:
                continue
        return sorted(out)


class BookingCancellation(models.Model):
    """
    One thing given back off a booking: the whole stay called off, some of its
    nights given up, or an extra service dropped from the nights it hadn't been
    delivered on yet.

    A booking can be trimmed more than once — two nights this week, another
    three later — so what was cancelled is a LIST of events, not a pair of
    columns. Each row is the frozen receipt for one of them: which nights went,
    what they were worth, what the policy kept and what went back to the guest,
    in the words the guest was shown at the time.

    Services sit here rather than beside the additions because this is the
    refund ledger — `refunded_total` is the sum of these rows — and money going
    back to the guest belongs in one place whatever it was for.

    The booking's own `cancelled_at` / `cancellation_fee` still describe the
    stay as a whole (the fee is the running total of every event's penalty), so
    nothing that only ever cared about "was this cancelled, and what did it
    cost" has to learn about the rows underneath.
    """

    KIND_FULL = "full"
    KIND_PARTIAL = "partial"
    KIND_SERVICES = "services"
    KIND_CHOICES = [
        (KIND_FULL, "Whole booking"),
        (KIND_PARTIAL, "Selected nights"),
        (KIND_SERVICES, "Extra services"),
    ]

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="cancellations"
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_PARTIAL)
    # The nights given up, as ISO dates in order. A full cancellation lists
    # every night the booking still held, so the two kinds read alike.
    #
    # A SERVICES row is the odd one out: the stay is untouched and every one of
    # these nights is still the guest's — they are the nights the service was
    # refunded OVER, which is what makes the amount checkable. That is why
    # `nights_count` stays 0 on it: no night was given up (see
    # Booking.cancelled_nights, which reads what the stay holds, not these rows).
    nights = models.JSONField(default=list, blank=True)
    nights_count = models.PositiveIntegerField(default=0)
    # On a SERVICES row, what was handed back: {"name", "price", "nights",
    # "amount"} each, frozen exactly as BookingAddition.services freezes a
    # purchase. Empty on the other two kinds, whose subject is nights.
    services = models.JSONField(default=list, blank=True)
    # What those nights were worth of the booking total, and how that split.
    # `stay_value` = `cancellation_fee` + `refund_amount`, always.
    stay_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refund_percentage = models.PositiveIntegerField(default=0)
    # How much of `refund_amount` was extra services on those nights. They come
    # back in full whatever the ladder charged on the stay itself, so a receipt
    # that didn't name them would look like the percentage was wrong. 0 on
    # cancellations taken before services could be bought per night.
    extras_refund = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # The policy line the guest read as they confirmed it.
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_bookingcancellation"
        ordering = ["created_at"]

    def __str__(self):
        return f"Cancellation #{self.pk} — booking {self.booking_id} ({self.kind})"

    def night_dates(self) -> list:
        out = []
        for raw in self.nights or []:
            try:
                out.append(date.fromisoformat(str(raw)[:10]))
            except ValueError:
                continue
        return sorted(out)


class CheckInVerification(models.Model):
    """
    One stay PIN: issued to a booking, shown only to the guest, entered by the
    host — on the way in AND on the way out (see `purpose`).

    The point is that a host cannot move a stay along on their own say-so — they
    have to prove the guest is standing there. The PIN is generated by the
    server, appears ONLY on the guest's own booking page, and the host has to be
    told it out loud to type it in. It lives for a minute, works once, and three
    wrong tries burn it and warn the guest by email.

    Every issue and every attempt is a row: this table is the check-in/out audit
    trail, alongside the `properties.checkin` log (see properties/checkin.py).
    Rows are never mutated except to record what happened to them.
    """

    PIN_LENGTH = 4
    PIN_TTL_SECONDS = 60
    MAX_FAILED_ATTEMPTS = 3

    # Which half of the stay this code proves. A departure is verified exactly
    # like an arrival — same digits, same minute, same three tries — and this is
    # what keeps the two apart: a code issued for one is never accepted for the
    # other, so a PIN the guest read out at the door can't close their stay.
    PURPOSE_CHECK_IN = "check_in"
    PURPOSE_CHECK_OUT = "check_out"
    PURPOSE_CHOICES = [
        (PURPOSE_CHECK_IN, "Check-in"),
        (PURPOSE_CHECK_OUT, "Check-out"),
    ]

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="check_in_verifications"
    )
    purpose = models.CharField(
        max_length=16, choices=PURPOSE_CHOICES, default=PURPOSE_CHECK_IN
    )
    # The 4 digits, as generated. Readable because the GUEST has to read it —
    # a one-way hash would be more comfortable but there would be nothing to
    # show them. It is never serialised into any owner-facing field (see
    # BookingType), it is worthless a minute after it is made, and it unlocks
    # nothing on its own: the host still has to be the villa's owner.
    pin = models.CharField(max_length=8)
    generated_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    # Set the moment the host types it correctly — the PIN is spent from then on.
    verified_at = models.DateTimeField(null=True, blank=True)
    # Set when the PIN is retired without being used: superseded by a fresh one,
    # or burned by three wrong entries. Together with `verified_at` this is what
    # makes a PIN single-use and replay-proof — `live_for()` will not look at a
    # row that carries either stamp.
    invalidated_at = models.DateTimeField(null=True, blank=True)
    # When the "someone is guessing your PIN" email went to the guest.
    alert_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "properties_checkin_verification"
        ordering = ["-generated_at"]
        indexes = [models.Index(fields=["booking", "-generated_at"])]

    def __str__(self):
        return f"{self.get_purpose_display()} PIN for booking #{self.booking_id}"

    @classmethod
    def new_pin(cls) -> str:
        """A fresh PIN from the system CSPRNG — never `random`, which is
        seeded predictably and would make PINs guessable in sequence."""
        return f"{secrets.randbelow(10 ** cls.PIN_LENGTH):0{cls.PIN_LENGTH}d}"

    @classmethod
    def live_for(cls, booking, now=None, purpose=None):
        """
        The booking's usable PIN right now, or None. Spent, superseded and
        expired rows can never come back — this is the replay guard.

        `purpose` narrows it to an arrival or a departure code; left off, it
        answers "is any code live on this booking?", which is what the guest's
        own page asks (there is only ever one, whatever it is for).
        """
        now = now or timezone.now()
        rows = cls.objects.filter(
            booking=booking,
            verified_at__isnull=True,
            invalidated_at__isnull=True,
            expires_at__gt=now,
        )
        if purpose is not None:
            rows = rows.filter(purpose=purpose)
        return rows.order_by("-generated_at").first()

    def is_live(self, now=None) -> bool:
        now = now or timezone.now()
        return (
            self.verified_at is None
            and self.invalidated_at is None
            and self.expires_at > now
        )

    def seconds_left(self, now=None) -> int:
        now = now or timezone.now()
        return max(0, int((self.expires_at - now).total_seconds()))

    @property
    def attempts_left(self) -> int:
        return max(0, self.MAX_FAILED_ATTEMPTS - int(self.failed_attempts))


class StayHold(models.Model):
    """
    Dates taken off the market while one guest pays for them.

    Between pressing "Confirm and Pay" and the payment coming back, the nights
    are neither free nor booked — and without something standing in that gap,
    two guests filling in card details at the same time both get told yes. A
    hold is that something: it occupies the nights exactly as a booking does
    (see `availability.taken_nights`), so the second guest is refused up front
    rather than charged for a stay they can't have.

    It is deliberately short-lived and self-clearing. A guest who closes the tab
    mid-payment leaves nothing behind that a host has to notice: the hold simply
    expires and the nights come back. The happy path never waits for that —
    `create_booking` converts it, and any failure releases it on the spot.
    """

    # How long a hold survives unattended. Long enough to type a card number
    # and for a gateway to answer; short enough that an abandoned checkout
    # doesn't keep a villa off the market for the rest of the evening.
    LIFETIME_MINUTES = 15

    villa = models.ForeignKey(Villa, on_delete=models.CASCADE, related_name="stay_holds")
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stay_holds"
    )

    # The same shape a booking uses, and for the same reason: a held stay can be
    # split around nights somebody else has, so its outer dates only bracket the
    # runs, and `segments` is what actually says which nights are held.
    check_in = models.DateField()
    check_out = models.DateField()
    segments = models.JSONField(default=list, blank=True)
    nights = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)

    expires_at = models.DateTimeField()
    # Set when the guest's payment failed, they went back, or they abandoned
    # checkout — the nights are free again from that moment.
    released_at = models.DateTimeField(null=True, blank=True)
    # Set when the payment went through and this hold became a real reservation.
    booking = models.OneToOneField(
        Booking,
        on_delete=models.CASCADE,
        related_name="stay_hold",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_stay_hold"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["villa", "expires_at"], name="stayhold_villa_expiry"),
        ]

    def __str__(self):
        return f"Hold on villa {self.villa_id} for {self.guest_id} until {self.expires_at}"

    def is_live(self, now=None) -> bool:
        """Is this hold still keeping its nights off the market?"""
        if self.released_at is not None or self.booking_id is not None:
            return False
        return (now or timezone.now()) < self.expires_at

    def seconds_left(self, now=None) -> int:
        return max(0, int((self.expires_at - (now or timezone.now())).total_seconds()))

    # A hold describes its nights exactly the way a booking does, so the two can
    # be read by the same code.
    stay_segments = Booking.stay_segments
    occupied_nights = Booking.occupied_nights


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

    # How long after posting a guest may still change what they wrote. Long
    # enough to fix a rating picked in haste or a typo; short enough that a
    # review the property has been judged on for weeks can't be quietly
    # rewritten — a host who acted on it deserves it to stay put.
    #
    # Measured from `created_at`, never `updated_at`: from the edit would make
    # every edit buy another day, and the window would never close.
    EDIT_WINDOW_HOURS = 24

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

    def editable_until(self):
        """The moment this review stops being editable."""
        return self.created_at + timedelta(hours=self.EDIT_WINDOW_HOURS)

    def can_edit(self, now=None) -> bool:
        """Whether the guest may still change it (see EDIT_WINDOW_HOURS)."""
        # An unsaved review has no created_at yet; it is being written now, so
        # of course it can be edited.
        if self.created_at is None:
            return True
        return (now or timezone.now()) < self.editable_until()
