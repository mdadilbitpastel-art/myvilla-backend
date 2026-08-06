"""
Taking an extra service back off a booking that has already been paid for.

The mirror of the services half of `properties/additions.py`. A guest who
ticked the airport pickup at checkout and no longer wants it should not have to
cancel their stay to be rid of it — they should be able to drop it and have the
money for it back.

What comes back is decided by the same line everything else on a live booking is
decided by: **a night that has begun is spent**. A service is something the host
delivers on a night, so the nights of it that have already started are the
host's — breakfast served on Tuesday is not refundable on Wednesday — and the
nights still ahead of the guest have not been delivered and come back IN FULL.
There is no sliding scale here: the cancellation ladder is about accommodation
held out of somebody else's reach, and a service the host has not yet started
costs them nothing to drop (the same reasoning `Booking.extras_for_nights`
already refunds services whole on a cancelled night).

What a removal DOESN'T touch is as deliberate as what it does. `total` and
`extras_total` stay exactly as they were, like they do on a night cancellation:
they are the frozen price of what was bought, every per-night figure is worked
out against them, and the money that went back is a row in the refund ledger
(see BookingCancellation) rather than a quiet edit to the price. What the
removal does change is what the service still COVERS — `refunded` on the entry,
read by `Booking.service_live_nights` — so the same money can never come back
twice, and a stay extended afterwards doesn't carry a service the guest dropped.

Everything is priced on the server's clock at the moment it is asked, exactly
like a cancellation, so a quote the guest reads is the arithmetic the button
performs.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List

from django.utils import timezone

from properties.additions import blocked_reason
from properties.models import Booking, BookingCancellation

MSG_NONE_LEFT = "This booking has no extra services to remove."
MSG_ALL_STARTED = (
    "Every night these services cover has already begun, so there is nothing "
    "left to refund."
)
MSG_PICK = "Choose the services you'd like to remove."


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class RemovalQuote:
    """What dropping a set of extra services would hand back, right now."""

    # Each service as it would be refunded: {"name", "price", "nights", "amount",
    # "kept", "from"} — `nights` being the nights coming back, `kept` the ones
    # already begun that stay bought and stay charged.
    services: List[dict] = field(default_factory=list)
    # Every night being refunded, across all of them, in date order. The stay
    # still holds these — only the service on them is going.
    nights: List[date] = field(default_factory=list)
    amount: Decimal = Decimal("0.00")
    allowed: bool = False
    error: str = ""
    message: str = ""

    @property
    def nights_count(self) -> int:
        return len(self.nights)


def removable_services(booking: Booking, now=None) -> List[dict]:
    """
    Every extra service on this booking that can still be dropped, and what
    dropping it would give back.

    The nights are sorted into three, and only one of them is money:

      * `dates` — nights the stay STILL HOLDS whose check-in hour hasn't passed.
        These are what the guest is refunded for, and nothing else is.
      * `kept` — nights the stay holds that are already under way. Delivered, so
        they stay bought and stay charged.
      * nights the guest has already CANCELLED. The service on them came back
        with the night (see `Booking.extras_for_nights`), so refunding them here
        would hand the same money over twice. They are not in the figure — but
        the service does stop covering them, which is `drop` below.

    A service with nothing ahead of it is left out entirely rather than offered
    as a row worth $0 — there is nothing there to refund, and a screen that says
    otherwise with a button is a screen that lies.
    """
    now = now or timezone.now()
    held = booking.occupied_nights()
    out = []
    for entry in booking.live_service_entries():
        price = _money(entry.get("price", 0) or 0)
        covered = sorted(booking.service_covered_nights(entry))
        ahead = [n for n in covered if n in held and now < booking.night_starts_at(n)]
        kept = [n for n in covered if n in held and n not in set(ahead)]
        if price <= 0 or not ahead:
            continue
        out.append(
            {
                "name": str(entry.get("name", "")).strip(),
                "price": float(price),
                "nights": len(ahead),
                "amount": float(_money(price * len(ahead))),
                "kept": len(kept),
                "from": ahead[0],
                "dates": ahead,
                # Everything the service stops covering: the nights being paid
                # back, and the cancelled ones it was still nominally running
                # over. Leaving those behind would let a night bought back later
                # arrive with a service on it the guest no longer has.
                "drop": sorted(set(covered) - set(kept)),
            }
        )
    return out


def quote_removal(booking: Booking, names, now=None) -> RemovalQuote:
    """
    Price dropping these services from `booking`, at the server's clock.

    Names the booking doesn't have, services already given back, duplicates and
    ones with no night left ahead of them are all dropped rather than refused —
    the client sends names, the booking's own frozen entries decide what they are
    worth, and nothing a tampered page sends can invent a service, a price or a
    night that has already been slept through.
    """
    now = now or timezone.now()
    quote = RemovalQuote()

    blocked = blocked_reason(booking, now)
    if blocked:
        quote.error = blocked
        return quote

    if not booking.live_service_entries():
        quote.error = MSG_NONE_LEFT
        return quote

    offered = {s["name"].strip().lower(): s for s in removable_services(booking, now)}
    if not offered:
        quote.error = MSG_ALL_STARTED
        return quote

    nights = set()
    seen = set()
    for raw in names or []:
        key = str(raw or "").strip().lower()
        service = offered.get(key)
        if service is None or key in seen:
            continue
        seen.add(key)
        quote.services.append(
            {
                "name": service["name"],
                "price": service["price"],
                "nights": service["nights"],
                "amount": service["amount"],
                "kept": service["kept"],
                "from": service["from"],
                # What is paid back, and what the service stops covering — the
                # two differ by any night the guest had already cancelled.
                "dates": service["dates"],
                "drop": service["drop"],
            }
        )
        nights.update(service["dates"])

    if not quote.services:
        quote.error = MSG_PICK
        return quote

    quote.nights = sorted(nights)
    quote.amount = _money(sum(Decimal(str(s["amount"])) for s in quote.services))
    quote.allowed = True

    count = len(quote.nights)
    kept = sum(s["kept"] for s in quote.services)
    quote.message = (
        f"Refunded in full for the {count} night{'' if count == 1 else 's'} "
        "that haven't started yet."
        + (
            f" The {kept} night{'' if kept == 1 else 's'} already under way "
            f"{'stays' if kept == 1 else 'stay'} charged."
            if kept
            else ""
        )
    )
    return quote


def apply_removal(booking: Booking, quote: RemovalQuote, now=None) -> BookingCancellation:
    """
    Carry out the removal `quote` describes and return its refund receipt.

    Call inside a transaction; the caller saves nothing else.

    The entry is kept, not deleted. What it was charged over is frozen onto it
    first (`nights`) so a later extension can't quietly re-price a service that
    is no longer running; `refunded_nights` names the dates it stops covering,
    and `refunded_amount` records what was actually paid back — which is not the
    same arithmetic, since a night the guest had already cancelled stops being
    covered without a penny changing hands here. Keeping the row is also what
    keeps `extras_total` and `total` true: they still say what the guest was
    charged, and the refund is the ledger row beside them.
    """
    if not quote.allowed:
        raise ValueError(quote.error or MSG_PICK)
    now = now or timezone.now()

    entries = booking.service_entries()
    by_name = {
        str(e.get("name", "")).strip().lower(): e
        for e in entries
        if not booking.service_removed(e)
    }
    for service in quote.services:
        entry = by_name.get(service["name"].strip().lower())
        if entry is None:
            continue
        # Freeze the billed count before anything reads it as a fallback: a
        # service ticked at checkout carries none of its own, and the fallback
        # is the stay's night count, which the next extension would move.
        entry["nights"] = booking.service_nights(entry)
        entry["refunded_nights"] = [
            n.isoformat()
            for n in sorted(
                booking.service_refunded_night_set(entry) | set(service["drop"])
            )
        ]
        entry["refunded_amount"] = float(
            booking.service_refunded_value(entry) + Decimal(str(service["amount"]))
        )
        entry["removed_at"] = now.isoformat()
        entry["removed_from"] = service["from"].isoformat()
    booking.extra_services = entries

    record = BookingCancellation.objects.create(
        booking=booking,
        kind=BookingCancellation.KIND_SERVICES,
        # The nights refunded OVER, not nights given up — the stay is unchanged
        # and still holds every one of them (hence the 0 beneath).
        nights=[n.isoformat() for n in quote.nights],
        nights_count=0,
        # What was given back, frozen — so the receipt names it rather than
        # leaving a reader to pick the names back out of the sentence.
        services=[
            {
                "name": s["name"],
                "price": s["price"],
                "nights": s["nights"],
                "amount": s["amount"],
            }
            for s in quote.services
        ],
        # `stay_value` is what left the booking's frozen total, and all of it
        # went back: a service the host hasn't started keeps no percentage.
        stay_value=quote.amount,
        cancellation_fee=Decimal("0.00"),
        refund_amount=quote.amount,
        refund_percentage=100,
        extras_refund=quote.amount,
        message=(
            ", ".join(s["name"] for s in quote.services)
            + " removed. "
            + quote.message
        )[:300],
    )
    booking.save(update_fields=["extra_services", "updated_at"])
    # Same reason `apply_nights_cancellation` drops its own: this request is
    # about to build a response off a relation it read before this row existed.
    getattr(booking, "_prefetched_objects_cache", {}).pop("cancellations", None)
    return record
