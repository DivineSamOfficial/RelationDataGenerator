"""From placed order to CONFIRMED (1D §2-3; D5, D47, D49, D53; 1E P08; step 3.8a).

* **Prepaid** (``ADVANCE``: B2C online and store pickup): ``PENDING_PAYMENT``; 1-3 gateway attempts (UPI, card,
  net banking), each failing with ``payment_failure_share``; a customer may give up after a failure. A SUCCESS
  confirms the order and posts **P08** Dr Payment Clearing / Cr AR (customer) with an unapplied AR payment
  item (D49, D53); when every attempt fails the order is cancelled ``PAYMENT_FAILED``. Failed attempts post
  nothing. Gateway fees are booked at settlement (P10, bank module).
* **Cash on delivery**: ``PENDING_CREDIT_CHECK`` → CONFIRMED seconds later (automatic COD eligibility check,
  1D §2 flow). **Counter sales** are CONFIRMED when keyed in (paid at handover, 3.9).
* **B2B on credit** (D47): ``PENDING_CREDIT_CHECK``, then the automatic check: overdue invoices beyond the grace
  period → ``ON_HOLD`` (OVERDUE_BALANCE); open AR + this order above the credit limit → ``ON_HOLD``
  (CREDIT_LIMIT); otherwise CONFIRMED. Held orders are reviewed every working morning: released when the
  check passes, released by a Finance override (an approver within limit, reason ``Finance override``) on some
  days, cancelled ``CREDIT_HOLD_EXPIRED`` after ``hold_expiry_days``.

The AR balance used by the check is Σ open AR items (excluding reversed ones). Allocations move amounts between
a customer's own items, so this equals the customer's AR control balance (V-AR-01) at the time of the check.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import load_account_book, post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver, record_status
from scmgen.generation.engine.sales.demand import Draft
from scmgen.generation.engine.sales.model import SalesModel
from scmgen.generation.numbering import Registration

OVERRIDE_REASON = "Finance override"
PAYMENT_FAILURES = ("INSUFFICIENT_FUNDS", "BANK_DECLINED", "TIMEOUT", "AUTH_FAILED")


class Checkout:
    """Per-day resources of the checkout: series registration, collection account, methods, AR balances."""

    def __init__(self, day: DayContext, model: SalesModel) -> None:
        self.day = day
        self.model = model
        principal = day.rows("""SELECT r.company_gst_registration_id, s.state_code FROM company_gst_registrations r
                                JOIN states s ON s.state_id = r.state_id WHERE r.is_principal_place""")[0]
        self.registration = Registration(principal["company_gst_registration_id"], principal["state_code"])
        self.bank = day.rows("SELECT bank_account_id FROM bank_accounts WHERE account_purpose = 'COLLECTIONS' "
                             "AND account_status = 'ACTIVE' ORDER BY bank_account_id LIMIT 1")[0]["bank_account_id"]
        self.methods = {r["method_code"]: r for r in day.rows(
            "SELECT payment_method_id, method_code, settlement_lag_days, gateway_fee_percent, max_amount "
            "FROM payment_methods WHERE usable_for_receipts")}
        self.book = load_account_book(day.conn)
        self.end_of_day = datetime.combine(day.day, time(23, 59, 59))
        grace = day.day - timedelta(days=day.world.config.sales.overdue_grace_days)
        self.ar: dict[int, dict[str, Any]] = {r["customer_id"]: r for r in day.rows("""
            SELECT customer_id, SUM(open_amount) AS balance,
                   SUM(item_type = 'INVOICE' AND open_amount > 0 AND due_date < :g) AS overdue
            FROM accounts_receivable WHERE item_status <> 'REVERSED' GROUP BY customer_id""", g=grace)}

    def clamp(self, moment: datetime) -> datetime:
        return min(moment, self.end_of_day)

    def hold_reason(self, customer_id: int, credit_limit: Decimal | None, amount: Decimal) -> str | None:
        """D47: OVERDUE_BALANCE, CREDIT_LIMIT or None (passes)."""
        ar = self.ar.get(customer_id)
        if ar and ar["overdue"]:
            return "OVERDUE_BALANCE"
        balance = ar["balance"] if ar else Decimal(0)
        if credit_limit is not None and balance + amount > credit_limit:
            return "CREDIT_LIMIT"
        return None


# ------------------------------------------------------------------------------------------ new orders


def place(day: DayContext, checkout: Checkout, draft: Draft) -> None:
    """Insert one order with its lines, run its checkout and record every status it passed through."""
    model = checkout.model
    order_id = day.ids.next("orders")
    rng = day.rng(f"sales.checkout.{order_id}")
    number = day.numbers.next(day.conn, "SO", day.day, checkout.registration)
    term = model.payment_terms[draft.payment_term]
    estimate = draft.totals["order_total_estimate"]
    placed = draft.placed_at
    events: list[tuple[str, datetime, int | None, str | None]] = []
    hold, cancel = None, None
    attempts: list[dict[str, Any]] = []
    if term["is_prepaid"]:
        events.append(("PENDING_PAYMENT", placed, None, None))
        attempts, outcome, at = _prepay(day, rng, checkout, order_id, draft.customer.customer_id, estimate, placed)
        if outcome == "SUCCESS":
            events.append(("CONFIRMED", at, None, "Payment received"))
        else:
            events.append(("CANCELLED", at, None, "All payment attempts failed"))
            cancel = ("PAYMENT_FAILED", at)
    elif draft.customer.kind == "B2B":
        events.append(("PENDING_CREDIT_CHECK", placed, None, None))
        checked = checkout.clamp(placed + timedelta(minutes=rng.randint(*day.world.config.sales.credit_check_minutes)))
        hold = checkout.hold_reason(draft.customer.customer_id, draft.customer.credit_limit, estimate)
        events.append(("ON_HOLD", checked, None, f"Credit check: {hold}") if hold
                      else ("CONFIRMED", checked, None, "Credit check passed"))
    elif draft.fulfilment == "COUNTER_SALE":
        events.append(("CONFIRMED", placed, draft.sales_employee_id, "Counter sale"))
    else:                                  # cash on delivery: automatic COD eligibility check (1D §2)
        events.append(("PENDING_CREDIT_CHECK", placed, None, None))
        events.append(("CONFIRMED", checkout.clamp(placed + timedelta(seconds=rng.randint(1, 20))), None,
                       "COD check passed"))
    status, last = events[-1][0], events[-1][1]
    model_channels = model.sales_channels
    refs = day.world.refs
    day.writer.insert("orders", {
        "order_id": order_id, "order_number": number, "customer_id": draft.customer.customer_id,
        "order_placed_at": placed, "order_date": day.day,
        "sales_channel_id": model_channels[draft.sales_channel]["sales_channel_id"],
        "marketing_channel_id": refs["marketing_channel"][draft.marketing_channel],
        "marketing_campaign_id": draft.campaign_id, "fulfilment_type": draft.fulfilment,
        "store_warehouse_id": draft.store_id, "billing_address_id": draft.customer.billing_address_id,
        "shipping_address_id": draft.shipping_address_id, "payment_term_id": term["payment_term_id"],
        "sales_employee_id": draft.sales_employee_id, "customer_po_reference": draft.customer_po_reference,
        "requested_delivery_date": draft.requested_delivery_date, "prices_include_tax": draft.customer.kind == "B2C",
        **draft.totals, "order_status": status, "hold_reason": hold if status == "ON_HOLD" else None,
        "cancelled_at": cancel[1] if cancel else None, "cancellation_reason": cancel[0] if cancel else None,
        "created_at": placed, "updated_at": last,
    })
    for line in draft.lines:
        day.writer.insert("order_line_items", {
            "order_line_item_id": day.ids.next("order_line_items"), "order_id": order_id, **line,
            "quantity_cancelled": line["quantity_ordered"] if cancel else Decimal(0),
            "line_status": "CANCELLED" if cancel else "OPEN", "created_at": placed, "updated_at": last,
        })
    previous = None
    for to_status, at, by, reason in events:
        record_status(day, "order", order_id, previous, to_status, at, by, reason)
        previous = to_status
    for payment in attempts:                 # after the order row: posting may flush the buffer
        day.writer.insert("customer_payments", payment)
        if payment["payment_status"] == "SUCCESS":
            _post_receipt(day, checkout, payment["customer_payment_id"], payment["customer_id"], payment["amount"],
                          payment["completed_at"])


def _prepay(day: DayContext, rng: random.Random, checkout: Checkout, order_id: int, customer_id: int,
            amount: Decimal, placed: datetime) -> tuple[list[dict[str, Any]], str, datetime]:
    """Gateway attempts until one succeeds or the customer gives up: (payment rows, outcome, decision time)."""
    settings = day.world.config.sales
    moment = placed + timedelta(seconds=rng.randint(30, 180))
    rows: list[dict[str, Any]] = []
    for attempt in range(1, settings.max_payment_attempts + 1):
        code = rng.choices(list(settings.prepaid_methods), weights=list(settings.prepaid_methods.values()))[0]
        method = checkout.methods[code]
        if method["max_amount"] is not None and amount > method["max_amount"]:
            method = checkout.methods["CARD"]
        initiated = checkout.clamp(moment)
        completed = checkout.clamp(initiated + timedelta(seconds=rng.randint(5, 90)))
        failed = rng.random() < settings.payment_failure_share
        payment_id = day.ids.next("customer_payments")
        fee = (money(amount * method["gateway_fee_percent"] / 100) if method["gateway_fee_percent"] else Decimal(0))
        rows.append({
            "customer_payment_id": payment_id,
            "payment_number": day.numbers.next(day.conn, "RCPT", day.day, checkout.registration),
            "customer_id": customer_id, "order_id": order_id, "payment_type": "ORDER_PREPAYMENT",
            "attempt_number": attempt, "initiated_at": initiated, "payment_date": day.day,
            "payment_method_id": method["payment_method_id"], "bank_account_id": checkout.bank, "amount": amount,
            "gateway_fee_amount": fee, "collected_by_shipping_carrier_id": None,
            "gateway_reference": f"X{method['method_code'][:3]}{day.day:%y%m%d}{payment_id:09d}",
            "payment_status": "FAILED" if failed else "SUCCESS",
            "failure_reason": rng.choice(PAYMENT_FAILURES) if failed else None, "completed_at": completed,
            "settlement_date": None if failed else day.day + timedelta(days=method["settlement_lag_days"]),
            "settlement_bank_transaction_id": None, "reversed_at": None, "reversal_reason": None,
            "created_at": initiated, "updated_at": completed,
        })
        if not failed:
            return rows, "SUCCESS", completed
        if attempt == settings.max_payment_attempts or rng.random() >= settings.payment_retry_share:
            return rows, "FAILED", checkout.clamp(completed + timedelta(minutes=rng.randint(15, 60)))
        moment = completed + timedelta(seconds=rng.randint(60, 600))
    raise AssertionError("unreachable")


def _post_receipt(day: DayContext, checkout: Checkout, payment_id: int, customer_id: int, amount: Decimal,
                  at: datetime) -> None:
    """P08 for a gateway receipt (D53: into Payment Clearing) and its unapplied AR item (D49)."""
    book = checkout.book
    journal = (JournalBuilder(f"Customer payment {payment_id}")
               .debit(book.role("PAYMENT_CLEARING"), amount)
               .credit(book.role("AR_CONTROL"), amount, customer_id=customer_id).build())
    journal_id = post_journal(day, journal, source="CUSTOMER_PAYMENT", customer_payment_id=payment_id)
    day.writer.insert("accounts_receivable", {
        "accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": customer_id,
        "item_type": "PAYMENT", "invoice_id": None, "credit_note_id": None, "customer_payment_id": payment_id,
        "refund_id": None, "document_date": day.day, "due_date": day.day, "original_amount": -amount,
        "open_amount": -amount, "item_status": "OPEN", "settled_at": None, "journal_entry_id": journal_id,
        "created_at": at, "updated_at": at,
    })


# ------------------------------------------------------------------------------------------ credit holds


def review_holds(day: DayContext, checkout: Checkout) -> None:
    """Every working morning, Credit Control looks at the held orders (D47)."""
    if not day.is_working_day:
        return
    settings = day.world.config.sales
    held = day.rows("""
        SELECT o.order_id, o.customer_id, o.order_total_estimate, o.updated_at, c.credit_limit FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'ON_HOLD' ORDER BY o.order_id""")
    if not held:
        return
    rng = day.rng("sales.holds")
    for order in held:
        at = day.at(10, rng.randint(0, 59), rng.randint(0, 59))
        reason = checkout.hold_reason(order["customer_id"], order["credit_limit"], order["order_total_estimate"])
        if reason is None:
            _release(day, order["order_id"], at, None, "Credit check passed")
        elif (day.day - order["updated_at"].date()).days >= settings.hold_expiry_days:
            day.execute("UPDATE orders SET order_status = 'CANCELLED', hold_reason = NULL, cancelled_at = :t, "
                        "cancellation_reason = 'CREDIT_HOLD_EXPIRED', updated_at = :t WHERE order_id = :o",
                        {"t": at, "o": order["order_id"]})
            day.execute("UPDATE order_line_items SET quantity_cancelled = quantity_ordered - quantity_shipped, "
                        "line_status = 'CANCELLED', updated_at = :t WHERE order_id = :o",
                        {"t": at, "o": order["order_id"]})
            record_status(day, "order", order["order_id"], "ON_HOLD", "CANCELLED", at, None,
                          f"Credit hold expired after {settings.hold_expiry_days} days")
        elif rng.random() < settings.hold_override_share:
            by = approver(day, order["order_total_estimate"], ("FIN",), day.day)
            _release(day, order["order_id"], at, by, OVERRIDE_REASON)


def _release(day: DayContext, order_id: int, at: datetime, by: int | None, reason: str) -> None:
    day.execute("UPDATE orders SET order_status = 'CONFIRMED', hold_reason = NULL, updated_at = :t "
                "WHERE order_id = :o", {"t": at, "o": order_id})
    record_status(day, "order", order_id, "ON_HOLD", "CONFIRMED", at, by, reason)
