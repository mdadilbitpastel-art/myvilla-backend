"""
Stay verification — issuing PINs, checking them, and the audit trail.

The rule this file exists to enforce: a host cannot move a stay along by
pressing a button. They press it, the server makes a 4-digit PIN, the PIN
appears ONLY on the guest's own booking page, and the host has to be told it
to type it in. So a check-in is evidence that the guest was actually there —
and a check-out is evidence they were there to leave.

Both ends of the stay work the same way, and `purpose` is the only thing that
separates them: a code issued for an arrival is never accepted for a departure.

The one deliberate exception is a departure at or past its booked hour. The
check-out code exists to stop a host putting a guest out EARLY, and once that
hour is behind us it protects nothing: the host closes the stay in one press
(`check_out_without_pin`), and if nobody does, the platform closes it half an
hour later on its own (`sync_forced_check_out`). Both are recorded as what they
are — the audit trail names the host or names the platform.

Everything about that is deliberately narrow:

* the PIN lives one minute and works once — an old code can never be replayed;
* three wrong entries burn it and email the guest a security alert;
* a burned or expired PIN is not an error the host can't recover from — they
  press the button again and a brand-new PIN is issued, as long as the stay is
  still at the point that code is for;
* every issue and every attempt is written to the `properties.checkin` logger
  and left behind as a CheckInVerification row, so a disputed check-in or
  check-out can be reconstructed months later.

The model holds the data and the small predicates; the orchestration lives
here, so the mutations stay thin and any other caller (an admin action, a
management command) gets exactly the same rules.
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import CheckInVerification

logger = logging.getLogger("properties.checkin")


CHECK_IN = CheckInVerification.PURPOSE_CHECK_IN
CHECK_OUT = CheckInVerification.PURPOSE_CHECK_OUT


class CheckInError(Exception):
    """A refusal whose message is safe to show the host as it is."""


def _gate(booking, purpose, now):
    """
    Whether the stay is at the point this PIN is for, and the line to show the
    host when it isn't. One place, so issuing a code and verifying it can never
    disagree about whether it was allowed.
    """
    if purpose == CHECK_OUT:
        gate = booking.check_out_gate(now)
        return gate.available, gate.message
    gate = booking.check_in_gate(now)
    return gate.checkin_available, gate.message


def _audit(event: str, booking, *, actor=None, **fields) -> None:
    """
    One line per check-in event, for auditing.

    Never logs the PIN itself: the log is read by more people, and for longer,
    than the minute the code is worth anything.
    """
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "checkin event=%s booking=%s villa=%s guest=%s actor=%s %s",
        event,
        booking.pk,
        booking.villa_id,
        booking.guest_id,
        getattr(actor, "pk", None),
        parts,
    )


def issue_pin(booking, *, purpose=CHECK_IN, actor=None, now=None) -> CheckInVerification:
    """
    Start a check-in or a check-out: retire whatever PIN this booking had and
    mint a new one for `purpose`.

    Refused unless the stay is at the point that code is for — the same gates
    the dashboard buttons read, so a stale page or a hand-made request can't get
    past what the UI shows.
    """
    now = now or timezone.now()
    available, message = _gate(booking, purpose, now)
    if not available:
        raise CheckInError(message)
    # Past the booked hour a departure needs no code (see
    # Booking.check_out_pin_required), so there is nothing to issue: minting one
    # would put a PIN on the guest's page for a step neither side has to take,
    # and the guest has very likely already left.
    if purpose == CHECK_OUT and not booking.check_out_pin_required(now):
        raise CheckInError(
            "The booked check-out time has passed — no PIN is needed. Press "
            "Check out to close this stay."
        )

    with transaction.atomic():
        # Supersede any live PIN, whatever it was for. Only one code is ever
        # valid for a booking, so a second button press can't leave the first
        # code working behind it.
        superseded = CheckInVerification.objects.filter(
            booking=booking, verified_at__isnull=True, invalidated_at__isnull=True
        ).update(invalidated_at=now)
        verification = CheckInVerification.objects.create(
            booking=booking,
            purpose=purpose,
            pin=CheckInVerification.new_pin(),
            expires_at=now + timedelta(seconds=CheckInVerification.PIN_TTL_SECONDS),
        )

    _audit(
        "pin_generated",
        booking,
        actor=actor,
        purpose=purpose,
        verification=verification.pk,
        superseded=superseded or None,
        expires_at=verification.expires_at.isoformat(),
    )
    return verification


def headcount(booking, guests) -> int:
    """
    The number of people the host says are walking in, checked against the
    villa.

    Refused above the property's capacity — the listing promises a place that
    sleeps N, and a host who could type any number would be recording a stay
    the property can't hold (and, in a dispute months later, a figure nobody
    could stand behind). One place, so the dialog's `max` and what the server
    will actually accept can never drift apart.
    """
    try:
        count = int(str(guests).strip())
    except (TypeError, ValueError):
        raise CheckInError("Enter how many guests are checking in.")
    if count < 1:
        raise CheckInError("At least one guest has to be checking in.")
    capacity = booking.guest_capacity()
    if count > capacity:
        raise CheckInError(
            f"This property sleeps {capacity} guest"
            f"{'' if capacity == 1 else 's'} — you can't check in {count}."
        )
    return count


def verify_pin(
    booking, pin: str, *, purpose=CHECK_IN, guests=None, actor=None, now=None
) -> CheckInVerification:
    """
    Check the PIN the host typed and, if it's right, check the guest in or out.

    On the way in the host also states how many people are arriving; it is
    validated BEFORE the PIN is looked at, so a mistyped headcount costs the
    host a sentence rather than one of the code's three lives.

    Raises CheckInError on every refusal — expired, wrong, or locked — with the
    line to show the host. On success the booking carries the arrival or
    departure stamp and the PIN is spent.
    """
    now = now or timezone.now()
    # The button the host would press to recover — named in every refusal that
    # is recoverable, so "try again" always points at something real.
    press = "Check in" if purpose == CHECK_IN else "Check out"

    # Asked of the PART in front of us: a guest who finished part one is not
    # "already checked in" for part two — that arrival is still to come.
    if purpose == CHECK_IN and booking.current_part_checked_in_at() is not None:
        raise CheckInError("This guest is already checked in.")

    available, message = _gate(booking, purpose, now)
    if not available:
        # The window shut (or the stay moved on) between generating the PIN and
        # typing it in.
        _audit("pin_window_closed", booking, actor=actor, purpose=purpose)
        raise CheckInError(message)

    # After the gates (a stay that can't be checked into at all should say so
    # rather than argue about the headcount) and before the PIN is read, so a
    # number the property can't hold never costs one of the code's three tries.
    arriving = headcount(booking, guests) if purpose == CHECK_IN else None

    verification = CheckInVerification.live_for(booking, now, purpose=purpose)
    if verification is None:
        _audit("pin_expired", booking, actor=actor, purpose=purpose)
        raise CheckInError(
            f"That PIN is no longer valid. Press {press} again to send the "
            "guest a new one."
        )

    entered = (pin or "").strip()
    # compare_digest, not ==: a plain comparison returns as soon as two digits
    # differ, and the time it took is a (small) hint about how much was right.
    if not secrets.compare_digest(entered, verification.pin):
        verification.failed_attempts += 1
        fields = ["failed_attempts"]
        locked = verification.failed_attempts >= CheckInVerification.MAX_FAILED_ATTEMPTS
        if locked:
            verification.invalidated_at = now
            fields.append("invalidated_at")
        verification.save(update_fields=fields)

        if locked:
            _send_guest_alert(booking, verification, now=now)
            _audit(
                "pin_locked",
                booking,
                actor=actor,
                verification=verification.pk,
                attempts=verification.failed_attempts,
            )
            raise CheckInError(
                "Invalid PIN — this code is locked after "
                f"{CheckInVerification.MAX_FAILED_ATTEMPTS} failed attempts and "
                f"the guest has been notified. Press {press} again for a new PIN."
            )

        _audit(
            "pin_failed",
            booking,
            actor=actor,
            verification=verification.pk,
            attempts=verification.failed_attempts,
        )
        left = verification.attempts_left
        raise CheckInError(
            f"Invalid PIN. {left} attempt{'' if left == 1 else 's'} left before "
            "this code is locked."
        )

    with transaction.atomic():
        # Spend the PIN and move the stay along together: neither half of this
        # should ever be true without the other.
        verification.verified_at = now
        verification.invalidated_at = now
        verification.save(update_fields=["verified_at", "invalidated_at"])
        if purpose == CHECK_OUT:
            released = _record_departure(booking, now)
        else:
            released = 0
            _record_arrival(booking, now, arriving)

    _audit(
        "pin_verified",
        booking,
        actor=actor,
        purpose=purpose,
        verification=verification.pk,
        late=booking.late_check_in_allowed or None,
        released_nights=released or None,
        guests=arriving,
    )
    return verification


def _record_arrival(booking, now, guests=None) -> None:
    """Stamp the arrival the verified PIN just proved, and who walked in with
    it. Inside the caller's transaction."""
    # Stamp the PART the guest is arriving for. On a split stay this is the
    # second (or third) arrival, and the booking-level column keeps its plain
    # meaning — the FIRST time they turned up — so it is only set once, by
    # part one.
    part = booking.current_part(now)
    fields = ["segment_stays", "updated_at"]
    booking.record_part_stay(
        part["index"] if part else 1, checked_in_at=now, guests=guests
    )
    # The headcount is NOT like the arrival stamp beside it: every part
    # overwrites it, because it answers "how many people are in the property",
    # and after the second arrival that is the second party, not the first.
    if guests is not None:
        booking.checked_in_guests = int(guests)
        fields.append("checked_in_guests")
    if booking.checked_in_at is None:
        booking.checked_in_at = now
        fields.append("checked_in_at")
    booking.save(update_fields=fields)


def _record_departure(booking, now) -> int:
    """
    Close off the part the guest is leaving, and hand back the nights they
    aren't going to use. Returns how many nights that released.

    A guest who leaves early is not owed anything — the stay was paid for in
    full and going home is their own decision — but the property is empty from
    that moment, and nobody is served by it sitting unbookable. So the money is
    left exactly as it is and the calendar is corrected: the part is shortened
    to the day they actually left, which is what puts those nights back on sale.

    On a split stay this closes ONE part. The guest is still due back for the
    next one, so only that part's remaining nights are released and the booking
    itself is finished only when there is no part left outstanding.
    """
    part = booking.current_part(now)
    if part is None:
        return 0
    ends_on, released = booking.departure_release(now)

    booking.record_part_stay(part["index"], checked_out_at=now)
    fields = ["segment_stays", "updated_at"]
    if released:
        booking.shorten_part(part["index"], ends_on)
        booking.released_nights = int(booking.released_nights or 0) + released
        fields += ["segments", "released_nights"]
    # Asked AFTER the shortening, because that is what decides it: with the part
    # closed and its tail given back, is anything still outstanding? The
    # booking-level stamp means the FINAL departure, so it waits for that.
    if booking.current_part(now) is None:
        booking.checked_out_at = now
        fields.append("checked_out_at")
    booking.save(update_fields=fields)
    return released


def check_out_without_pin(booking, *, actor=None, now=None) -> int:
    """
    Close a stay whose booked check-out hour has passed, on the host's say-so
    and without a code.

    The middle ground between the two things that already existed: before the
    hour a departure needs the guest's PIN, and half an hour after it the
    platform closes the stay itself. In between, the host pressing Check out is
    recording something that is already true — the hour is up, the guest owes
    the property nothing more, and the only alternative on offer is waiting for
    the clock to do the identical thing with nobody's name on it. A host who is
    standing in the empty villa should not need to phone a guest who has left in
    order to say so.

    Refused while the hour is still ahead: that is precisely the case the PIN
    exists for, and this is not a way around it.

    Returns the nights released — always 0 here, since a departure at or past
    the booked hour has no unused nights to hand back. Kept as the return value
    so this reads the same way as `_record_departure` for its callers.
    """
    now = now or timezone.now()

    gate = booking.check_out_gate(now)
    if not gate.available:
        raise CheckInError(gate.message)
    if booking.check_out_pin_required(now):
        raise CheckInError(
            "The booked check-out time hasn't passed yet — ask the guest for "
            "the 4-digit PIN on their booking to check them out now."
        )

    with transaction.atomic():
        # Any live departure code dies with the stay it was for. Leaving one
        # breathing would be a code that outlives the thing it proves, and the
        # guest is still looking at it on their own booking page.
        CheckInVerification.objects.filter(
            booking=booking,
            purpose=CHECK_OUT,
            verified_at__isnull=True,
            invalidated_at__isnull=True,
        ).update(invalidated_at=now)
        released = _record_departure(booking, now)

    _audit("check_out_no_pin", booking, actor=actor, released_nights=released or None)
    return released


def sync_forced_check_out(booking, *, now=None) -> bool:
    """
    Close a stay nobody closed: the booked check-out hour has passed and the
    half-hour grace after it has run out.

    No PIN, and deliberately. The code was only ever there to stop a host
    ending a stay BEFORE its hour; once that hour is behind us there is nothing
    left for it to protect, and holding the booking open until two people
    happen to be in the same place with a phone helps nobody — the guest is
    gone, the property is free, and the record says otherwise.

    Written lazily, the first time the booking is read after the deadline, for
    the same reason as `sync_no_show`: nothing is waiting on the stroke of the
    half hour, so there is no scheduler. What that costs is that the stamp must
    not be "now" — a booking first looked at three days later would claim the
    guest walked out three days late — so the departure is recorded at the
    DEADLINE, which is when it actually became true.

    Returns True when it closed something (so callers can log it once).
    """
    now = now or timezone.now()
    due_at = booking.auto_check_out_at(now)
    if due_at is None or now < due_at:
        return False

    with transaction.atomic():
        # At `due_at` the part is still open, so this closes exactly the part
        # that overran — and releases nothing, because a departure past the
        # booked hour has no unused nights to give back.
        released = _record_departure(booking, due_at)
        booking.forced_check_out_at = due_at
        booking.save(update_fields=["forced_check_out_at", "updated_at"])

    _audit(
        "forced_check_out",
        booking,
        due_at=due_at.isoformat(),
        released_nights=released or None,
    )
    return True


def allow_late_check_in(booking, *, actor=None, now=None) -> None:
    """
    The host's decision to take a guest in after the window shut.

    Re-opens check-in — through the same PIN verification, so a late arrival is
    still proved rather than asserted. The refund stays 0%: the guest missed the
    window they agreed to, and the host choosing to be accommodating about the
    room is not the same as the platform refunding the stay.
    """
    now = now or timezone.now()
    if booking.late_check_in_allowed:
        return
    booking.late_check_in_allowed = True
    booking.save(update_fields=["late_check_in_allowed", "updated_at"])
    _audit("late_checkin_allowed", booking, actor=actor)


def _send_guest_alert(booking, verification, *, now=None) -> None:
    """
    Warn the guest that someone is guessing their stay PIN.

    Best-effort: a mail server that's down must not stop the platform from
    refusing the check-in, which is the part that actually protects the guest.
    The failure is logged rather than swallowed silently.
    """
    now = now or timezone.now()
    email = (booking.contact_email or booking.guest.email or "").strip()
    if not email:
        return

    villa = booking.villa_title or booking.villa.title
    # Named for what the code was actually for, so a guest who is mid-stay isn't
    # told someone is guessing their way IN.
    leaving = verification.purpose == CHECK_OUT
    what = "check-out" if leaving else "check-in"
    try:
        send_mail(
            subject=f"Security Alert: Multiple Invalid {what.title()} Attempts",
            message=(
                f"Hi {(booking.guest.full_name or '').strip() or 'there'},\n\n"
                f"We detected {CheckInVerification.MAX_FAILED_ATTEMPTS} invalid "
                f"attempts to verify your property {what} PIN for your stay at "
                f"{villa}.\n\n"
                "If this wasn't expected, please contact the property owner or "
                "customer support immediately.\n\n"
                "If it was simply mistyped, no action is needed — ask the host "
                f"to start {what} again and a new PIN will appear on your "
                "booking.\n\n"
                "— MyVilla"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.exception(
            "Check-in PIN alert email failed (booking=%s, backend=%s)",
            booking.pk,
            settings.EMAIL_BACKEND,
        )
        return

    verification.alert_sent_at = now
    verification.save(update_fields=["alert_sent_at"])
    _audit("guest_alert_sent", booking, verification=verification.pk)
