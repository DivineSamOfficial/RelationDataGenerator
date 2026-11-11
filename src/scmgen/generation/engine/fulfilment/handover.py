"""Store handovers: counter sales and store pickups (1D §2; D4, D49, D53; 1E P06, P08; step 3.9a).

Neither has a shipment. The store issues the reserved shelf stock and a tax invoice at the moment the goods
change hands (place of supply = the store's state), and the order goes straight to ``DELIVERED``:

* **Counter sale**: a few minutes after the till keys the order in, the customer pays the invoice total
  (``COUNTER_PAYMENT``; cash, UPI or card by ``counter_methods``, within each method's limit). Cash goes to the
  store's petty-cash float, UPI and card into Payment Clearing (D53); P08 posts it, and the payment is allocated
  to the invoice at once, so both AR items are settled.
* **Store pickup**: the prepaid order is collected ``pickup_delay_days`` after it was confirmed (drawn per order,
  D65), on a working day during store hours; the prepayment is allocated to the invoice.

A line whose shelf stock turns out to be expired is handed over in part (the rest of its reservation is
released and the quantity cancelled).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.invoicing import BillLine, Invoicer, ar_state
from scmgen.generation.engine.fulfilment.orders import OrderBook
from scmgen.generation.engine.fulfilment.stock import Issuer, ledger_row
from scmgen.generation.numbering import Registration

EXPIRED_REASON = "Shelf stock expired"


def pickup_day(day: DayContext, order: dict[str, Any]):
    """The day the customer comes to collect a store-pickup order (drawn once, keyed by the order)."""
    rng = day.streams.stream(order["order_date"], f"fulfilment.pickup.{order['order_id']}")
    return order["order_date"] + timedelta(days=rng.randint(*day.world.config.fulfilment.pickup_delay_days))


def handover_step(day: DayContext, invoicer: Invoicer) -> None:
    book = OrderBook.load(day, """
        o.fulfilment_type IN ('COUNTER_SALE', 'STORE_PICKUP') AND o.order_status = 'CONFIRMED'""")
    due = []
    for order in book.orders.values():
        if order["fulfilment_type"] == "COUNTER_SALE":
            due.append(order)
        elif day.is_working_day and day.day >= max(pickup_day(day, order), order["updated_at"].date()):
            due.append(order)
    if not due:
        return
    reservations = [r for order in due for r in book.reservations_of(order["order_id"])]
    invoicer.load(due, {r["product_id"] for r in reservations})
    issuer = Issuer(day, {(r["warehouse_id"], r["product_id"]) for r in reservations})
    till = _Till(day, invoicer)
    end_of_day = datetime.combine(day.day, time(23, 59, 59))
    planned = []
    for order in due:
        rng = day.rng(f"fulfilment.handover.{order['order_id']}")
        if order["fulfilment_type"] == "COUNTER_SALE":
            at = order["updated_at"] + timedelta(minutes=rng.randint(*day.world.config.fulfilment.handover_minutes),
                                                 seconds=rng.randint(0, 59))
        else:
            at = max(day.at(rng.randint(11, 19), rng.randint(0, 59), rng.randint(0, 59)),
                     order["updated_at"] + timedelta(minutes=30))
        arrived = [a for a in (issuer.arrived_at(r["warehouse_id"], r["product_id"])
                               for r in book.reservations_of(order["order_id"])) if a is not None]
        at = max(at, max(arrived, default=at) + timedelta(minutes=5))
        planned.append((min(at, end_of_day), order["order_id"], order, rng))
    for at, _, order, rng in sorted(planned, key=lambda p: (p[0], p[1])):     # issues in time order (MAC)
        _hand_over(day, invoicer, issuer, till, book, order, at, rng)
    issuer.write()
    book.write_orders("DELIVERED")


def _hand_over(day: DayContext, invoicer: Invoicer, issuer: Issuer, till: _Till, book: OrderBook,
               order: dict[str, Any], at: datetime, rng) -> None:
    store = order["store_warehouse_id"]
    bill_lines, ledger = [], []
    for reservation in book.reservations_of(order["order_id"]):
        qty = issuer.pickable(store, reservation["product_id"], reservation["outstanding_quantity"], day.day)
        if qty > 0:
            line = BillLine(book.lines[reservation["order_line_item_id"]], qty, Decimal(0))
            pieces = issuer.issue(store, reservation["product_id"], qty, at)
            line.cost = sum((-p.movement.value_change for p in pieces), Decimal(0))
            bill_lines.append(line)
            ledger.append((line, reservation["product_id"], pieces))
            book.fulfil(reservation, qty, at, handover=True)
        book.release_rest(reservation, at, EXPIRED_REASON)
    if not bill_lines:
        return
    bill = invoicer.bill(order, store, invoicer.warehouses[store]["state_id"], None, at, bill_lines,
                         completes_order=book.completes(order["order_id"]))
    for line, product_id, pieces in ledger:
        for piece in pieces:
            day.writer.insert("inventory_transactions", ledger_row(day, store, product_id, piece, at,
                                                                   invoice_line_item_id=line.invoice_line_item_id))
    if order["fulfilment_type"] == "COUNTER_SALE":
        till.collect(rng, order, store, bill.invoice_id, bill.total_amount, at)


class _Till:
    """Counter payments: method, receiving account (store float or the collections account) and P08."""

    def __init__(self, day: DayContext, invoicer: Invoicer) -> None:
        self.day = day
        self.invoicer = invoicer
        self.methods = {r["method_code"]: r for r in day.rows(
            "SELECT payment_method_id, method_code, settlement_lag_days, gateway_fee_percent, max_amount "
            "FROM payment_methods WHERE usable_for_receipts")}
        self.collections = day.rows("SELECT bank_account_id FROM bank_accounts WHERE account_purpose = 'COLLECTIONS' "
                                    "AND account_status = 'ACTIVE' ORDER BY bank_account_id LIMIT 1")[0]["bank_account_id"]
        self.floats = {r["warehouse_id"]: r["bank_account_id"] for r in day.rows(
            "SELECT warehouse_id, bank_account_id FROM bank_accounts WHERE account_type = 'PETTY_CASH'")}

    def collect(self, rng, order: dict[str, Any], store: int, invoice_id: int, amount: Decimal, at: datetime) -> None:
        day, invoicer = self.day, self.invoicer
        mix = day.world.config.fulfilment.counter_methods
        code = rng.choices(list(mix), weights=list(mix.values()))[0]
        for candidate in (code, "UPI", "CARD"):
            limit = self.methods[candidate]["max_amount"]
            if limit is None or amount <= limit:
                code = candidate
                break
        method = self.methods[code]
        cash = code == "CASH"
        account = self.floats[store] if cash else self.collections
        payment_id = day.ids.next("customer_payments")
        warehouse = invoicer.warehouses[store]
        registration = invoicer.registrations[warehouse["company_gst_registration_id"]]
        fee = money(amount * method["gateway_fee_percent"] / 100) if method["gateway_fee_percent"] else Decimal(0)
        initiated = at - timedelta(seconds=rng.randint(20, 90))
        day.writer.insert("customer_payments", {
            "customer_payment_id": payment_id,
            "payment_number": day.numbers.next(day.conn, "RCPT", day.day, Registration(
                registration["company_gst_registration_id"], registration["state_code"])),
            "customer_id": order["customer_id"], "order_id": order["order_id"], "payment_type": "COUNTER_PAYMENT",
            "attempt_number": 1, "initiated_at": initiated, "payment_date": day.day,
            "payment_method_id": method["payment_method_id"], "bank_account_id": account, "amount": amount,
            "gateway_fee_amount": fee, "collected_by_shipping_carrier_id": None,
            "gateway_reference": None if cash else f"X{code[:3]}{day.day:%y%m%d}{payment_id:09d}",
            "payment_status": "SUCCESS", "failure_reason": None, "completed_at": at,
            "settlement_date": day.day + timedelta(days=method["settlement_lag_days"]),
            "settlement_bank_transaction_id": None, "reversed_at": None, "reversal_reason": None,
            "created_at": initiated, "updated_at": at,
        })
        book = invoicer.book
        debit = book.bank(account) if cash else book.role("PAYMENT_CLEARING")
        journal = (JournalBuilder(f"Counter payment {payment_id}")
                   .debit(debit, amount)
                   .credit(book.role("AR_CONTROL"), amount, customer_id=order["customer_id"]).build())
        journal_id = post_journal(day, journal, source="CUSTOMER_PAYMENT", customer_payment_id=payment_id)
        state = ar_state(-amount, Decimal(0), at)
        day.writer.insert("accounts_receivable", {
            "accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": order["customer_id"],
            "item_type": "PAYMENT", "invoice_id": None, "credit_note_id": None, "customer_payment_id": payment_id,
            "refund_id": None, "document_date": day.day, "due_date": day.day, "original_amount": -amount,
            "open_amount": Decimal(0), "item_status": state["item_status"], "settled_at": state["settled_at"],
            "journal_entry_id": journal_id, "created_at": at, "updated_at": at,
        })
        day.writer.insert("customer_payment_allocations", {
            "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
            "customer_payment_id": payment_id, "credit_note_id": None, "invoice_id": invoice_id, "refund_id": None,
            "allocated_amount": amount, "allocation_date": day.day, "allocated_by_employee_id": None,
            "allocation_status": "ACTIVE", "reversed_at": None, "created_at": at, "updated_at": at,
        })
        invoicer.settle_invoice(invoice_id, amount, at)
