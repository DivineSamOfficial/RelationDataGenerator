"""The carrier's side of a shipment (1D §4; D40, D41, D43; 1E P08; step 3.9b).

**Journey.** Each shipment's whole journey is planned once, from a stream keyed by the shipment (D65), so every
run that reaches a day sees the same events:

* hub carriers: PICKED_UP scan at the warehouse, ARRIVED_AT_HUB, DEPARTED_HUB after ``linehaul_hours`` (our status
  becomes ``IN_TRANSIT``), an IN_TRANSIT scan on long routes, ARRIVED_AT_HUB at the destination state's hub, then
  OUT_FOR_DELIVERY and the delivery attempt on the delivery day (expected date, ``late_share`` later; no Sunday
  deliveries); hyperlocal riders: pickup, on the way (``IN_TRANSIT``), out for delivery, the same or next day;
* attempts: the first fails with ``first_attempt_failure`` (higher for COD), a repeat with
  ``repeat_attempt_failure``; a failure is a refusal with ``refusal_share`` (COD mostly); a refusal or the third
  failure starts the return to origin: RTO_INITIATED the next morning, RTO_IN_TRANSIT, RTO_DELIVERED back at the
  warehouse after the same transit time (our status ``RETURNED_TO_SENDER``, handled by ``rto.py``).

**Feed.** Every scan reaches our system ``feed_lag_minutes`` later, within the same day (clean mode, lag ≤ 6 h);
the tracking row is recorded then (``created_at = received_at``). The shipment's status changes at the scan time
(the carrier's business time) when the scan arrives. How far a shipment has got is read from its stored events
(D66): each day writes the planned events received that day.

**Delivery.** A delivered attempt records the receiver and an OTP reference, delivers the shipment's lines
(order-line delivered counters and statuses); for a COD parcel the courier collects the invoice total in cash: a
``COD_COLLECTION`` payment (Payment Clearing until the carrier's remittance ``cod_remittance_days`` later, D43, D53),
P08, an AR payment item allocated to the shipment's invoice.

Customer cancellation after dispatch but before the parcel left the origin hub cancels the invoice (D40,
``cancellation.py``); the same plan stream as the pre-dispatch sweep (``sales.cancel.<order>``) decides it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.dispatch import _place, transit_days
from scmgen.generation.engine.fulfilment.invoicing import Invoicer, ar_state
from scmgen.generation.engine.sales.allocation import _distance, cancellation_plan
from scmgen.generation.numbering import Registration

IN_FLIGHT = ("PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERY_FAILED")


@dataclass
class Event:
    code: str
    description: str
    at: datetime
    city: str | None
    state_id: int | None
    status: str | None = None              # our shipment status from this scan on
    attempt: dict[str, Any] | None = None  # delivery attempt recorded with this scan
    sequence: int = 0
    received: datetime | None = None


@dataclass
class Journey:
    events: list[Event] = field(default_factory=list)
    departs: datetime | None = None        # the scan that makes the shipment IN_TRANSIT
    returns: bool = False


def _not_sunday(day: date) -> date:
    return day + timedelta(days=1) if day.weekday() == 6 else day


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute))


def _hub(day: DayContext, state_id: int, fallback: str) -> str:
    """The destination state's hub: its biggest market city."""
    hubs = day.world.refs.setdefault("state_hubs", {})
    if state_id not in hubs:
        code = day.rows("SELECT state_code FROM states WHERE state_id = :s", s=state_id)[0]["state_code"]
        cities = [c for c in day.world.seed.cities if c["state_code"] == code]
        hubs[state_id] = max(cities, key=lambda c: (c["weight"], c["city"]))["city"] if cities else None
    return hubs[state_id] or fallback


def journey(day: DayContext, shipment: dict[str, Any]) -> Journey:
    """The shipment's planned journey (same in every run, D65)."""
    settings = day.world.config.fulfilment
    rng = day.streams.stream(shipment["planned_dispatch_date"], f"fulfilment.carrier.{shipment['shipment_id']}")
    origin, destination = _place(day, shipment["origin_city"]), _place(day, shipment["destination_city"])
    km = _distance(origin, destination) if origin and destination else 500.0
    kind = shipment["carrier_type"]
    cod = bool(shipment["is_cod"])
    plan = Journey()
    events = plan.events

    def add(code: str, description: str, at: datetime, city: str | None, state_id: int | None, **extra: Any) -> Event:
        if events and at <= events[-1].at:
            at = events[-1].at + timedelta(minutes=rng.randint(1, 10))
        event = Event(code, description, at, city, state_id, **extra)
        events.append(event)
        return event

    o_city, o_state = shipment["origin_city"], shipment["origin_state_id"]
    d_city, d_state = shipment["destination_city"], shipment["destination_state_id"]
    picked = add("PICKED_UP", f"Shipment picked up at {o_city}", shipment["dispatched_at"]
                 + timedelta(minutes=rng.randint(2, 15)), o_city, o_state)
    if kind == "HYPERLOCAL":
        plan.departs = add("IN_TRANSIT", "Rider on the way", picked.at + timedelta(minutes=rng.randint(5, 20)),
                           o_city, o_state, status="IN_TRANSIT").at
        first = _not_sunday(plan.departs.date() if plan.departs.hour < 18 else plan.departs.date() + timedelta(days=1))
    else:
        add("ARRIVED_AT_HUB", f"Arrived at {o_city} hub", picked.at + timedelta(minutes=rng.randint(60, 180)),
            o_city, o_state)
        plan.departs = add("DEPARTED_HUB", f"Departed from {o_city} hub",
                           picked.at + timedelta(hours=rng.randint(*settings.linehaul_hours), minutes=rng.randint(0, 59)),
                           o_city, o_state, status="IN_TRANSIT").at
        first = max(shipment["expected_delivery_date"],
                    plan.departs.date() + timedelta(days=0 if plan.departs.hour < 8 else 1))
        if rng.random() < settings.late_share:
            first += timedelta(days=rng.randint(*settings.late_days))
        first = _not_sunday(first)
        if km > 300:
            add("IN_TRANSIT", "In transit", plan.departs + (_at(first, 4, 0) - plan.departs) / 2, None, None)
        hub = _hub(day, d_state, d_city)
        add("ARRIVED_AT_HUB", f"Arrived at {hub} hub", _at(first, 4, rng.randint(0, 59))
            + timedelta(minutes=rng.randint(0, 90)), hub, d_state)

    # Delivery attempts.
    attempt_day = first
    for number in (1, 2, 3):
        out = add("OUT_FOR_DELIVERY", f"Out for delivery in {d_city}", _at(attempt_day, rng.randint(8, 10),
                  rng.randint(0, 59)), d_city, d_state, status="OUT_FOR_DELIVERY")
        latest = int((_at(out.at.date(), 20, 30) - out.at).total_seconds() // 60)    # riders work until 20:30
        tried = out.at + timedelta(minutes=rng.randint(min(30, max(latest, 5)), max(latest, 5)))
        failure = settings.first_attempt_failure["COD" if cod else "PREPAID"] if number == 1 \
            else settings.repeat_attempt_failure
        if rng.random() >= failure:
            add("DELIVERED", f"Delivered in {d_city}", tried, d_city, d_state, status="DELIVERED",
                attempt={"number": number, "outcome": "DELIVERED", "reason": None,
                         "otp": f"OTP-{rng.randint(100000, 999999)}"})
            return plan
        refused = rng.random() < settings.refusal_share["COD" if cod else "PREPAID"]
        if refused:
            reason = "REFUSED"
        else:
            reasons = {k: v for k, v in settings.failure_reasons.items() if cod or k != "COD_CASH_NOT_READY"}
            reason = rng.choices(list(reasons), weights=list(reasons.values()))[0]
        add("DELIVERY_ATTEMPTED", f"Delivery attempted: {reason.replace('_', ' ').lower()}", tried, d_city, d_state,
            status="DELIVERY_FAILED", attempt={"number": number, "outcome": "FAILED", "reason": reason, "otp": None})
        if refused:
            break
        attempt_day = _not_sunday(attempt_day + timedelta(days=1))

    # Return to origin.
    plan.returns = True
    started = add("RTO_INITIATED", "Return to origin initiated",
                  _at(_not_sunday(events[-1].at.date() + timedelta(days=1)), rng.randint(9, 11), rng.randint(0, 59)),
                  d_city, d_state)
    add("RTO_IN_TRANSIT", "Return in transit to origin", started.at + timedelta(hours=rng.randint(2, 10)), None, None)
    back = _not_sunday(started.at.date() + timedelta(days=transit_days(day, kind, km)))
    add("RTO_DELIVERED", f"Returned to origin at {o_city}", _at(back, rng.randint(10, 16), rng.randint(0, 59)),
        o_city, o_state, status="RETURNED_TO_SENDER")
    return plan


def _feed(day: DayContext, shipment: dict[str, Any], plan: Journey) -> None:
    """Number the events and give each its arrival time in our system (same day, lag ≤ 6 h)."""
    rng = day.streams.stream(shipment["planned_dispatch_date"], f"fulfilment.feed.{shipment['shipment_id']}")
    lag = day.world.config.fulfilment.feed_lag_minutes
    previous = None
    for number, event in enumerate(plan.events, 1):
        event.sequence = number
        received = min(event.at + timedelta(minutes=rng.randint(*lag), seconds=rng.randint(0, 59)),
                       datetime.combine(event.at.date(), time(23, 59, 59)))
        event.received = max(received, previous) if previous else received   # the feed arrives in order
        previous = event.received


# ------------------------------------------------------------------------------------------ the day


def in_flight(day: DayContext, dispatched_today: bool) -> list[dict[str, Any]]:
    """Shipments with the carrier: dispatched before today (morning pass) or today (after the dispatch wave)."""
    statuses = ", ".join(f"'{s}'" for s in IN_FLIGHT)
    when = "= :d" if dispatched_today else "< :d"
    rows = day.rows(f"""
        SELECT s.shipment_id, s.order_id, s.warehouse_id, s.shipping_carrier_id, s.tracking_number, s.is_cod,
               s.cod_amount, s.planned_dispatch_date, s.expected_delivery_date, s.dispatched_at, s.shipment_status,
               s.updated_at,
               c.carrier_type, w.city AS origin_city, w.state_id AS origin_state_id,
               a.city AS destination_city, a.state_id AS destination_state_id,
               o.customer_id, o.order_date, o.order_placed_at, o.order_status, k.customer_name, k.contact_person_name,
               k.customer_type, i.invoice_id, i.issued_at, i.company_gst_registration_id,
               r.accounts_receivable_id AS invoice_item_id, r.original_amount AS invoice_item_original,
               r.open_amount AS invoice_item_open,
               COALESCE((SELECT MAX(t.event_sequence) FROM shipment_tracking t WHERE t.shipment_id = s.shipment_id), 0)
                   AS last_sequence,
               (SELECT COUNT(*) FROM shipments x WHERE x.order_id = s.order_id AND x.shipment_status <> 'CANCELLED')
                   AS order_shipments,
               (SELECT COUNT(*) FROM customer_payments y WHERE y.order_id = s.order_id) AS order_payments
        FROM shipments s JOIN shipping_carriers c ON c.shipping_carrier_id = s.shipping_carrier_id
        JOIN warehouses w ON w.warehouse_id = s.warehouse_id
        JOIN customer_addresses a ON a.customer_address_id = s.shipping_address_id
        JOIN orders o ON o.order_id = s.order_id JOIN customers k ON k.customer_id = o.customer_id
        JOIN invoices i ON i.shipment_id = s.shipment_id AND i.invoice_status = 'ISSUED'
        JOIN accounts_receivable r ON r.invoice_id = i.invoice_id
        WHERE s.shipment_status IN ({statuses}) AND s.planned_dispatch_date {when}
        ORDER BY s.shipment_id""", d=day.day)
    for row in rows:
        row["invoice_item"] = {"accounts_receivable_id": row.pop("invoice_item_id"),
                               "original_amount": row.pop("invoice_item_original"),
                               "open_amount": row.pop("invoice_item_open")}
    return rows


@dataclass
class CarrierDay:
    """What the day's carrier events did, for the stock-moving follow-ups and the order updates."""
    delivered: list[tuple[dict[str, Any], datetime]] = field(default_factory=list)
    returned: list[tuple[dict[str, Any], datetime]] = field(default_factory=list)
    cancelled: list[tuple[dict[str, Any], datetime, str]] = field(default_factory=list)


def carrier_step(day: DayContext, invoicer: Invoicer, dispatched_today: bool) -> CarrierDay:
    """Write today's feed events for shipments in flight; collect deliveries, returns and cancellations."""
    result = CarrierDay()
    shipments = in_flight(day, dispatched_today)
    if not shipments:
        return result
    end_of_day = datetime.combine(day.day, time(23, 59, 59))
    collector = _CodCollector(day, invoicer)
    for shipment in shipments:
        plan = journey(day, shipment)
        _feed(day, shipment, plan)
        cancel = None
        if not dispatched_today and shipment["shipment_status"] == "PICKED_UP" and shipment["order_shipments"] == 1 \
                and shipment["order_status"] == "SHIPPED":
            planned = cancellation_plan(day, shipment["order_id"], shipment["order_date"], shipment["order_placed_at"])
            if planned and shipment["dispatched_at"] <= planned[0] < plan.departs \
                    and planned[0] <= shipment["issued_at"] + timedelta(hours=24) and planned[0].date() == day.day:
                cancel = planned
        status = shipment["shipment_status"]
        for event in plan.events:
            if event.sequence <= shipment["last_sequence"] or event.received > end_of_day:
                continue
            if event.received.date() != day.day:
                raise RuntimeError(f"Shipment {shipment['shipment_id']}: event {event.sequence} of "
                                   f"{event.received.date()} was not written on its day")
            if cancel and event.received >= cancel[0]:
                break                                # the order is cancelled before this scan arrives
            _write_event(day, shipment, event)
            if event.status and event.status != "RETURNED_TO_SENDER":
                _write_status(day, shipment, status, event)
                status = event.status
            if event.attempt:
                _write_attempt(day, shipment, event, collector)
            if event.status == "DELIVERED":
                result.delivered.append((shipment, event.at))
            elif event.status == "RETURNED_TO_SENDER":
                result.returned.append((shipment, event.at))
        if cancel:
            result.cancelled.append((shipment, cancel[0], cancel[1]))
    return result


def _write_event(day: DayContext, shipment: dict[str, Any], event: Event) -> None:
    day.writer.insert("shipment_tracking", {
        "shipment_tracking_id": day.ids.next("shipment_tracking"), "shipment_id": shipment["shipment_id"],
        "carrier_event_id": f"{shipment['tracking_number']}-{event.sequence:03d}", "event_sequence": event.sequence,
        "event_code": event.code, "event_description": event.description[:150], "event_at": event.at,
        "received_at": event.received, "location_city": event.city, "state_id": event.state_id,
        "created_at": event.received, "updated_at": event.received,
    })


def _write_status(day: DayContext, shipment: dict[str, Any], previous: str, event: Event) -> None:
    """Our status follows the scan (business time = scan time; recorded when the scan arrives)."""
    day.writer.insert("shipment_status_history", {
        "shipment_status_history_id": day.ids.next("shipment_status_history"),
        "shipment_id": shipment["shipment_id"], "from_status": previous, "to_status": event.status,
        "changed_at": event.at, "changed_by_employee_id": None, "change_reason": event.description[:255],
        "created_at": event.received, "updated_at": event.received,
    })
    values: dict[str, Any] = {"shipment_status": event.status, "updated_at": event.received}
    if event.status == "DELIVERED":
        values["delivered_at"] = event.at
    day.writer.update("shipments", "shipment_id", shipment["shipment_id"], values)


def _write_attempt(day: DayContext, shipment: dict[str, Any], event: Event, collector: _CodCollector) -> None:
    attempt = event.attempt
    delivered = attempt["outcome"] == "DELIVERED"
    receiver = (shipment["contact_person_name"] or shipment["customer_name"]) if delivered else None
    day.writer.insert("delivery_attempts", {
        "delivery_attempt_id": day.ids.next("delivery_attempts"), "shipment_id": shipment["shipment_id"],
        "attempt_number": attempt["number"], "attempted_at": event.at, "attempt_outcome": attempt["outcome"],
        "failure_reason": attempt["reason"], "received_by_name": receiver[:120] if receiver else None,
        "proof_of_delivery_reference": attempt["otp"],
        "cod_collected_amount": shipment["cod_amount"] if delivered and shipment["is_cod"] else None,
        "created_at": event.received, "updated_at": event.received,
    })
    if delivered and shipment["is_cod"]:
        collector.collect(shipment, event)


class _CodCollector:
    """COD (D43): the courier collects the invoice total at the door; the money sits in Payment Clearing until the
    carrier remits it (P10, bank module); P08 and the allocation to the shipment's invoice now."""

    def __init__(self, day: DayContext, invoicer: Invoicer) -> None:
        self.day = day
        self.invoicer = invoicer
        self.method = None
        self.bank = None
        self.attempts: dict[int, int] = {}          # order → payment attempts so far (a split COD order pays twice)

    def _load(self) -> None:
        day = self.day
        self.method = day.rows("SELECT payment_method_id FROM payment_methods WHERE method_code = 'CASH'")[0][
            "payment_method_id"]
        self.bank = day.rows("SELECT bank_account_id FROM bank_accounts WHERE account_purpose = 'COLLECTIONS' "
                             "AND account_status = 'ACTIVE' ORDER BY bank_account_id LIMIT 1")[0]["bank_account_id"]

    def collect(self, shipment: dict[str, Any], event: Event) -> None:
        if self.method is None:
            self._load()
        day, invoicer = self.day, self.invoicer
        amount, at, recorded = shipment["cod_amount"], event.at, event.received
        payment_id = day.ids.next("customer_payments")
        attempt = self.attempts.get(shipment["order_id"], shipment["order_payments"]) + 1
        self.attempts[shipment["order_id"]] = attempt
        registration = invoicer.registrations[shipment["company_gst_registration_id"]]
        day.writer.insert("customer_payments", {
            "customer_payment_id": payment_id,
            "payment_number": day.numbers.next(day.conn, "RCPT", day.day, Registration(
                registration["company_gst_registration_id"], registration["state_code"])),
            "customer_id": shipment["customer_id"], "order_id": shipment["order_id"], "payment_type": "COD_COLLECTION",
            "attempt_number": attempt, "initiated_at": at, "payment_date": at.date(), "payment_method_id": self.method,
            "bank_account_id": self.bank, "amount": amount, "gateway_fee_amount": Decimal(0),
            "collected_by_shipping_carrier_id": shipment["shipping_carrier_id"],
            "gateway_reference": shipment["tracking_number"], "payment_status": "SUCCESS", "failure_reason": None,
            "completed_at": at,
            "settlement_date": at.date() + timedelta(days=day.world.config.fulfilment.cod_remittance_days),
            "settlement_bank_transaction_id": None, "reversed_at": None, "reversal_reason": None,
            "created_at": recorded, "updated_at": recorded,
        })
        book = invoicer.book
        journal = (JournalBuilder(f"COD collection {payment_id}")
                   .debit(book.role("PAYMENT_CLEARING"), amount)
                   .credit(book.role("AR_CONTROL"), amount, customer_id=shipment["customer_id"]).build())
        journal_id = post_journal(day, journal, source="CUSTOMER_PAYMENT", customer_payment_id=payment_id)
        invoice_item = shipment["invoice_item"]
        applied = min(amount, invoice_item["open_amount"])
        state = ar_state(-amount, -amount + applied, recorded)
        day.writer.insert("accounts_receivable", {
            "accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": shipment["customer_id"],
            "item_type": "PAYMENT", "invoice_id": None, "credit_note_id": None, "customer_payment_id": payment_id,
            "refund_id": None, "document_date": at.date(), "due_date": at.date(), "original_amount": -amount,
            "open_amount": state["open_amount"], "item_status": state["item_status"], "settled_at": state["settled_at"],
            "journal_entry_id": journal_id, "created_at": recorded, "updated_at": recorded,
        })
        if applied > 0:
            day.writer.insert("customer_payment_allocations", {
                "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
                "customer_payment_id": payment_id, "credit_note_id": None, "invoice_id": shipment["invoice_id"],
                "refund_id": None, "allocated_amount": applied, "allocation_date": at.date(),
                "allocated_by_employee_id": None, "allocation_status": "ACTIVE", "reversed_at": None,
                "created_at": recorded, "updated_at": recorded,
            })
            invoice_item["open_amount"] -= applied
            day.writer.update("accounts_receivable", "accounts_receivable_id", invoice_item["accounts_receivable_id"],
                              ar_state(invoice_item["original_amount"], invoice_item["open_amount"], recorded))


__all__ = ["CarrierDay", "Journey", "carrier_step", "journey"]
