import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil

from django.conf import settings
from django.db import models
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
    # that moment (see cancellation_policy) — 0 when cancelled before the
    # check-in day, half the total on the day itself. Frozen at cancel time:
    # "now" keeps moving, but what the guest was charged does not.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Stamped the first time this booking is looked at after its check-in grace
    # period ran out with nobody checked in (see `sync_no_show`). The no-show
    # STATE is derived from the clock — it would be true whether or not anything
    # had written it down — but the moment it happened is worth keeping: it's
    # what a host, a support agent or a payout dispute needs months later.
    no_show_at = models.DateTimeField(null=True, blank=True)
    # The host's decision to take a no-show guest in anyway. Re-opens check-in
    # (with the same PIN verification) after the window has closed; the refund
    # stays 0% either way — the guest missed the window they agreed to.
    late_check_in_allowed = models.BooleanField(default=False)
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
    # Three tiers, decided by where "now" sits against this booking's own
    # check-in DATETIME (its check-in date at the check-in time frozen when the
    # guest booked). Percentages are of the booking total.
    #
    #   before the check-in day      → 100% back, no penalty
    #   on the day, before the time  →  50% back, 50% penalty
    #   at/after the check-in time   → cancelling is closed, 0% back
    #
    # The tier boundary is a moment, never a date: 11:59 PM the night before is
    # still a full refund, and 12:30 PM on the day of a 2 PM check-in is half.
    REFUND_BEFORE_CHECK_IN_DAY = 100
    REFUND_ON_CHECK_IN_DAY = 50
    REFUND_AFTER_CHECK_IN = 0

    MSG_FREE = "Free cancellation available."
    MSG_PARTIAL = "50% cancellation charge applies."
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
            }
        # A stay that predates per-part stamps (or was never split) carries its
        # arrival and departure on the booking itself — that IS part one's.
        if not out and (self.checked_in_at or self.checked_out_at):
            out[1] = {
                "checked_in_at": self.checked_in_at,
                "checked_out_at": self.checked_out_at,
            }
        return out

    @staticmethod
    def _parse_stamp(value):
        if not value:
            return None
        parsed = parse_datetime(str(value))
        return Booking._aware(parsed) if parsed else None

    def record_part_stay(self, index: int, *, checked_in_at=None, checked_out_at=None):
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

    def sync_no_show(self, now=None) -> bool:
        """
        Record the moment this booking became a no-show, if it just has.

        The no-show STATE is derived — `lifecycle_status` reads it off the clock
        whether or not anything wrote it down — so this exists purely to keep
        the timestamp, and it is written lazily, the first time the booking is
        looked at after the window shut. That avoids a scheduler for something
        no one is waiting on: nothing about the guest's or host's experience
        depends on a row being touched at 4:00:00 PM exactly.

        Returns True when it stamped (so callers can log it once).
        """
        now = now or timezone.now()
        if self.no_show_at is not None:
            return False
        if self.lifecycle_status(now) != self.LIFECYCLE_NO_SHOW:
            return False
        self.no_show_at = self.current_part_grace_ends_at()
        self.save(update_fields=["no_show_at", "updated_at"])
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

        # A part nobody arrived for whose check-out hour has passed is already
        # behind us — `current_part` has moved on to the next one, and the whole
        # stay running out that way is answered by the `stay_over` branch above.
        # So whatever we are looking at here is still running.
        #
        # Past the window, but the stay is still running. The host may take the
        # guest in by hand — a deliberate decision now, not the normal flow.
        if self.late_check_in_allowed:
            return gate(
                "No Show", visible=True, state="green", available=True, otp=True,
                message="Late check-in allowed — verify the guest's PIN to check them in.",
            )
        return gate(
            "No Show", visible=False, state="hidden", available=False,
            message="The guest did not check in within the allowed check-in window.",
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
        row. See the REFUND_* tiers above for the rule.

        The comparison is datetime against datetime, both timezone-aware:
        `now` is an instant (UTC in the database) and `check_in_datetime()` is
        the property's wall-clock check-in on the check-in date, made aware in
        the project timezone. Comparing dates alone would call 12:30 PM on the
        arrival day "expired" — hence the tier order below, which settles the
        moment first and only then asks which day it is.
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

        # 3. At or past the check-in moment — the window is shut.
        if now >= checkin_at:
            return CancellationPolicy.build(
                self.total,
                can_cancel=False,
                refund_percentage=self.REFUND_AFTER_CHECK_IN,
                message=self.MSG_EXPIRED,
            )

        # 2. Same calendar day as check-in, but the hour hasn't come round yet.
        #    `localtime` because the check-in date is a wall-clock date at the
        #    property; `now` is stored in UTC and would name the wrong day for
        #    part of every evening.
        if timezone.localtime(now).date() == self.check_in:
            return CancellationPolicy.build(
                self.total,
                can_cancel=True,
                refund_percentage=self.REFUND_ON_CHECK_IN_DAY,
                message=self.MSG_PARTIAL,
            )

        # 1. Any time before the check-in day — free.
        return CancellationPolicy.build(
            self.total,
            can_cancel=True,
            refund_percentage=self.REFUND_BEFORE_CHECK_IN_DAY,
            message=self.MSG_FREE,
        )

    def can_cancel(self, now=None):
        """Whether the guest may still call this stay off (see the policy)."""
        return self.cancellation_policy(now).can_cancel

    def cancel_fee_at(self, now=None):
        """The penalty a cancellation right now would carry, in currency."""
        return self.cancellation_policy(now).penalty_amount


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
