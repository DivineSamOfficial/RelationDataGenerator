"""Collections: B2B customers pay their invoices; cheques clear or bounce; card chargebacks (1D §3
``customer_payments``; 1E §5; D5, D49, D53; P08, P09; step 3.10).

* **Payment profile.** Every B2B customer has a stable profile drawn from a stream keyed by the customer (D65, like
  the demand traits): ``ON_TIME`` / ``SLIGHTLY_LATE`` / ``CHRONIC_LATE`` with their delay ranges after the due date,
  a weekly payment day, whether it pays by cheque, and whether it sometimes pays a round sum on account
  (``ADVANCE``).
* **Payment runs.** Each invoice gets a planned payment date (due date + the profile's delay, drawn per invoice).
  On its payment day a customer pays every invoice whose planned date has come, less what it already has on account
  or in uncleared cheques; some runs pay the last invoice only in part. NEFT / IMPS / RTGS (method limits) succeed
  the same day: P08 Dr Bank (collections account) / Cr AR. Cheques are INITIATED when received and clear after the
  method's lag (P08 then) or bounce (``CHEQUE_BOUNCED``, nothing posted; the customer pays them again).
* **Allocation (D49).** Payments, advances and credit notes are applied to the customer's open invoices oldest due
  date first, on every run; allocations post nothing.
* **Chargebacks.** A few card payments (prepaid orders, counter sales) are charged back 15–60 days later: the payment
  becomes REVERSED, P09 reverses its P08 (D61), its allocations are undone and the invoices are open again.

State lives in the documents (D66): open AR items, INITIATED cheques; plans are re-drawn from their streams.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money, whole_rupees
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal, reverse_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.invoicing import ar_state
from scmgen.generation.numbering import Registration

OPEN = ("OPEN", "PARTIALLY_SETTLED")


def profile(day: DayContext, customer_id: int, acquired: date) -> dict[str, Any]:
    """The customer's stable payment behaviour."""
    settings = day.world.config.collections
    rng = day.streams.stream(acquired, f"collections.customer.{customer_id}")
    kind = rng.choices(list(settings.profiles), weights=[p["share"] for p in settings.profiles.values()])[0]
    return {"kind": kind, "delay": settings.profiles[kind]["delay_days"], "weekday": rng.randint(0, 4),
            "cheque": rng.random() < settings.cheque_payer_share, "advance": rng.random() < settings.advance_payer_share}


def planned_payment(day: DayContext, invoice: dict[str, Any], behaviour: dict[str, Any]) -> date:
    rng = day.streams.stream(invoice["invoice_date"], f"collections.invoice.{invoice['invoice_id']}")
    return max(invoice["invoice_date"], invoice["due_date"] + timedelta(days=rng.randint(*behaviour["delay"])))


class _Collector:
    """Per-day resources: account book, collections account, receipt methods, the receipt number series."""

    def __init__(self, day: DayContext) -> None:
        self.day = day
        self.book = account_book(day)
        self.bank = day.rows("SELECT bank_account_id FROM bank_accounts WHERE account_purpose = 'COLLECTIONS' "
                             "AND account_status = 'ACTIVE' ORDER BY bank_account_id LIMIT 1")[0]["bank_account_id"]
        self.methods = {r["method_code"]: r for r in day.rows(
            "SELECT payment_method_id, method_code, settlement_lag_days, min_amount, max_amount FROM payment_methods "
            "WHERE usable_for_receipts")}
        principal = day.rows("""SELECT r.company_gst_registration_id, s.state_code FROM company_gst_registrations r
                                JOIN states s ON s.state_id = r.state_id WHERE r.is_principal_place""")[0]
        self.registration = Registration(principal["company_gst_registration_id"], principal["state_code"])

    def receipt(self, rng: random.Random, customer_id: int, payment_type: str, method_code: str, amount: Decimal,
                at: datetime, cleared: bool) -> dict[str, Any]:
        """A customer payment row; electronic transfers succeed at once, a cheque waits for clearing."""
        day = self.day
        method = self.methods[method_code]
        payment_id = day.ids.next("customer_payments")
        reference = (f"CHQ{rng.randint(100000, 999999)}" if method_code == "CHEQUE"
                     else f"XUTR{day.day:%y%m%d}{payment_id:010d}")
        row = {
            "customer_payment_id": payment_id,
            "payment_number": day.numbers.next(day.conn, "RCPT", day.day, self.registration),
            "customer_id": customer_id, "order_id": None, "payment_type": payment_type, "attempt_number": 1,
            "initiated_at": at, "payment_date": day.day, "payment_method_id": method["payment_method_id"],
            "bank_account_id": self.bank, "amount": amount, "gateway_fee_amount": Decimal(0),
            "collected_by_shipping_carrier_id": None, "gateway_reference": reference,
            "payment_status": "SUCCESS" if cleared else "INITIATED", "failure_reason": None,
            "completed_at": at if cleared else None, "settlement_date": day.day if cleared else None,
            "settlement_bank_transaction_id": None, "reversed_at": None, "reversal_reason": None,
            "created_at": at, "updated_at": at,
        }
        day.writer.insert("customer_payments", row)
        return row

    def post(self, payment_id: int, customer_id: int, amount: Decimal, document_date: date, at: datetime) -> dict:
        """P08 for a direct-to-bank receipt and its unapplied AR item (open credit until allocated)."""
        day, book = self.day, self.book
        journal = (JournalBuilder(f"Customer payment {payment_id}")
                   .debit(book.bank(self.bank), amount)
                   .credit(book.role("AR_CONTROL"), amount, customer_id=customer_id).build())
        journal_id = post_journal(day, journal, source="CUSTOMER_PAYMENT", customer_payment_id=payment_id)
        item = {"accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": customer_id,
                "original_amount": -amount, "open_amount": -amount, "document_date": document_date, "new": True,
                "customer_payment_id": payment_id, "credit_note_id": None}
        day.writer.insert("accounts_receivable", {
            "accounts_receivable_id": item["accounts_receivable_id"], "customer_id": customer_id, "item_type": "PAYMENT",
            "invoice_id": None, "credit_note_id": None, "customer_payment_id": payment_id, "refund_id": None,
            "document_date": document_date, "due_date": document_date, "original_amount": -amount,
            "open_amount": -amount, "item_status": "OPEN", "settled_at": None, "journal_entry_id": journal_id,
            "created_at": at, "updated_at": at,
        })
        return item


# ------------------------------------------------------------------------------------------ the day step


def collections_step(day: DayContext) -> None:
    if not day.is_working_day:
        return
    collector = _Collector(day)
    _chargebacks(day, collector)
    customers, invoices, credits, pending = _open_items(day)
    _clear_cheques(day, collector, invoices, credits)
    rng = day.rng("collections.runs")
    settings = day.world.config.collections
    for customer_id in sorted(customers):
        customer = customers[customer_id]
        behaviour = profile(day, customer_id, customer["acquisition_date"])
        if day.day.weekday() != behaviour["weekday"]:
            continue
        mine = sorted(invoices.get(customer_id, []), key=lambda i: (i["due_date"], i["invoice_id"]))
        due = [i for i in mine if planned_payment(day, i, behaviour) <= day.day]
        on_account = -sum((c["open_amount"] for c in credits.get(customer_id, [])), Decimal(0)) \
            + pending.get(customer_id, Decimal(0))
        amount = sum((i["open_amount"] for i in due), Decimal(0)) - on_account
        at = day.at(rng.randint(10, 16), rng.randint(0, 59), rng.randint(0, 59))
        if behaviour["advance"] and day.day.day <= 7 and mine and rng.random() < settings.advance_share:
            # A round sum on account, about half of what the customer has open (applied to the next invoices).
            advance = max(Decimal(10000), whole_rupees(sum(i["open_amount"] for i in mine) / 2 / 1000) * 1000)
            _pay(day, collector, rng, customer_id, behaviour, "ADVANCE", advance, at, invoices, credits, pending)
        if amount > 0 and len(due) > 1 and rng.random() < settings.partial_share:
            amount -= whole_rupees(due[-1]["open_amount"] * Decimal(str(rng.uniform(0.1, 0.6))))
        if amount > 0:
            _pay(day, collector, rng, customer_id, behaviour, "INVOICE_PAYMENT", amount, at, invoices, credits, pending)
        _allocate(day, credits.get(customer_id, []), mine, at)      # every credit on account, oldest invoice first


def _open_items(day: DayContext):
    """Open B2B invoices, open credits (payments on account, credit notes) and uncleared cheques per customer."""
    customers: dict[int, dict[str, Any]] = {}
    invoices: dict[int, list[dict[str, Any]]] = defaultdict(list)
    credits: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in day.rows(f"""
            SELECT a.accounts_receivable_id, a.customer_id, a.item_type, a.invoice_id, a.credit_note_id,
                   a.customer_payment_id, a.original_amount, a.open_amount, a.document_date, a.due_date,
                   c.acquisition_date, i.invoice_date
            FROM accounts_receivable a JOIN customers c ON c.customer_id = a.customer_id
            LEFT JOIN invoices i ON i.invoice_id = a.invoice_id
            WHERE a.item_status IN ({", ".join(f"'{s}'" for s in OPEN)}) AND c.customer_type = 'B2B'
              AND a.created_at < :today                -- today's invoices are paid on a later run
            ORDER BY a.customer_id, a.accounts_receivable_id""", today=day.at(0)):
        customers.setdefault(row["customer_id"], {"acquisition_date": row["acquisition_date"]})
        row["new"] = False
        (invoices if row["item_type"] == "INVOICE" else credits)[row["customer_id"]].append(row)
    pending = {r["customer_id"]: r["amount"] for r in day.rows("""
        SELECT p.customer_id, SUM(p.amount) AS amount FROM customer_payments p JOIN customers c USING (customer_id)
        WHERE p.payment_status = 'INITIATED' AND c.customer_type = 'B2B' GROUP BY p.customer_id""")}
    return customers, invoices, credits, pending


def _method(behaviour: dict[str, Any], rng: random.Random, amount: Decimal, collector: _Collector) -> str:
    if behaviour["cheque"]:
        return "CHEQUE"
    rtgs_min = collector.methods["RTGS"]["min_amount"] or Decimal(200000)
    imps_max = collector.methods["IMPS"]["max_amount"] or Decimal(500000)
    if amount >= rtgs_min:
        return "RTGS" if rng.random() < 0.6 else "NEFT"
    return "IMPS" if amount <= imps_max and rng.random() < 0.25 else "NEFT"


def _pay(day: DayContext, collector: _Collector, rng: random.Random, customer_id: int, behaviour: dict[str, Any],
         payment_type: str, amount: Decimal, at: datetime, invoices, credits, pending) -> None:
    method = _method(behaviour, rng, amount, collector)
    cleared = method != "CHEQUE"
    row = collector.receipt(rng, customer_id, payment_type, method, money(amount), at, cleared)
    if cleared:
        credits[customer_id].append(collector.post(row["customer_payment_id"], customer_id, row["amount"], day.day, at))
    else:
        pending[customer_id] = pending.get(customer_id, Decimal(0)) + row["amount"]


def _allocate(day: DayContext, credits: list[dict[str, Any]], invoices: list[dict[str, Any]], at: datetime) -> None:
    """Apply open credits (oldest first) to open invoices (oldest due date first); no journal (D49)."""
    for credit in sorted(credits, key=lambda c: (c["document_date"], c["accounts_receivable_id"])):
        for invoice in invoices:
            if credit["open_amount"] >= 0:
                break
            if invoice["open_amount"] <= 0:
                continue
            amount = min(-credit["open_amount"], invoice["open_amount"])
            day.writer.insert("customer_payment_allocations", {
                "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
                "customer_payment_id": credit["customer_payment_id"], "credit_note_id": credit["credit_note_id"],
                "invoice_id": invoice["invoice_id"], "refund_id": None, "allocated_amount": amount,
                "allocation_date": day.day, "allocated_by_employee_id": None, "allocation_status": "ACTIVE",
                "reversed_at": None, "created_at": at, "updated_at": at,
            })
            credit["open_amount"] += amount
            invoice["open_amount"] -= amount
            for item in (credit, invoice):
                day.writer.update("accounts_receivable", "accounts_receivable_id", item["accounts_receivable_id"],
                                  ar_state(item["original_amount"], item["open_amount"], at))
    credits[:] = [c for c in credits if c["open_amount"] < 0]


def _clear_cheques(day: DayContext, collector: _Collector, invoices, credits) -> None:
    """Cheques received earlier clear after the method's lag (P08 at clearance) or bounce."""
    settings = day.world.config.collections
    lag = collector.methods["CHEQUE"]["settlement_lag_days"]
    at = day.at(11, 30)
    for cheque in day.rows("""
            SELECT customer_payment_id, customer_id, amount, payment_date FROM customer_payments
            WHERE payment_status = 'INITIATED' AND payment_date <= :d ORDER BY customer_payment_id""",
                           d=day.day - timedelta(days=lag)):
        rng = day.streams.stream(cheque["payment_date"], f"collections.cheque.{cheque['customer_payment_id']}")
        if rng.random() < settings.cheque_bounce_share:
            day.writer.update("customer_payments", "customer_payment_id", cheque["customer_payment_id"], {
                "payment_status": "FAILED", "failure_reason": "CHEQUE_BOUNCED", "completed_at": at,
                "settlement_date": None, "updated_at": at})
            continue
        day.writer.update("customer_payments", "customer_payment_id", cheque["customer_payment_id"], {
            "payment_status": "SUCCESS", "failure_reason": None, "completed_at": at, "settlement_date": day.day,
            "updated_at": at})
        customer_id = cheque["customer_id"]
        credits[customer_id].append(collector.post(cheque["customer_payment_id"], customer_id, cheque["amount"],
                                                   day.day, at))
        _allocate(day, credits[customer_id], sorted(invoices.get(customer_id, []),
                                                    key=lambda i: (i["due_date"], i["invoice_id"])), at)


def _chargebacks(day: DayContext, collector: _Collector) -> None:
    """P09: a card payment charged back by the customer's bank; its allocations are undone (D49, D61). Never a
    payment whose invoice has a credit note (the customer was refunded or credited: nothing to dispute)."""
    settings = day.world.config.collections
    low, high = settings.chargeback_days
    at = day.at(12)
    for payment in day.rows("""
            SELECT p.customer_payment_id, p.customer_id, p.amount, p.payment_date, j.journal_entry_id,
                   a.accounts_receivable_id
            FROM customer_payments p JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
            JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id AND j.journal_source = 'CUSTOMER_PAYMENT'
            JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
            WHERE m.method_code = 'CARD' AND p.payment_status = 'SUCCESS' AND p.payment_date BETWEEN :a AND :b
              AND NOT EXISTS (SELECT 1 FROM customer_payment_allocations x JOIN credit_notes n ON n.invoice_id = x.invoice_id
                              WHERE x.customer_payment_id = p.customer_payment_id)
            ORDER BY p.customer_payment_id""", a=day.day - timedelta(days=high), b=day.day - timedelta(days=low)):
        rng = day.streams.stream(payment["payment_date"], f"collections.chargeback.{payment['customer_payment_id']}")
        if rng.random() >= settings.chargeback_share or \
                payment["payment_date"] + timedelta(days=rng.randint(low, high)) != day.day:
            continue
        reverse_journal(day, payment["journal_entry_id"], f"Chargeback of payment {payment['customer_payment_id']}")
        day.writer.update("customer_payments", "customer_payment_id", payment["customer_payment_id"], {
            "payment_status": "REVERSED", "reversed_at": at, "reversal_reason": "Card chargeback", "updated_at": at})
        reopened: dict[int, dict[str, Any]] = {}
        for allocation in day.rows("""
                SELECT x.customer_payment_allocation_id, x.allocated_amount, a.accounts_receivable_id,
                       a.original_amount, a.open_amount
                FROM customer_payment_allocations x JOIN accounts_receivable a ON a.invoice_id = x.invoice_id
                WHERE x.customer_payment_id = :p AND x.allocation_status = 'ACTIVE'
                ORDER BY x.customer_payment_allocation_id""", p=payment["customer_payment_id"]):
            day.writer.update("customer_payment_allocations", "customer_payment_allocation_id",
                              allocation["customer_payment_allocation_id"],
                              {"allocation_status": "REVERSED", "reversed_at": at, "updated_at": at})
            item = reopened.setdefault(allocation["accounts_receivable_id"], dict(allocation))
            item["open_amount"] += allocation["allocated_amount"]      # the invoice is open again by this much
        for item_id, item in sorted(reopened.items()):
            day.writer.update("accounts_receivable", "accounts_receivable_id", item_id,
                              ar_state(item["original_amount"], item["open_amount"], at))
        day.writer.update("accounts_receivable", "accounts_receivable_id", payment["accounts_receivable_id"], {
            "open_amount": Decimal(0), "item_status": "REVERSED", "settled_at": None, "updated_at": at})
