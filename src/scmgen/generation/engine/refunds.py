"""Refunds (1D §6 ``refunds``; R5, D44, D49, D61; 1E P09, P14; step 3.11b).

* **Credit notes the customer had paid for.** A consumer's credit note (return or return to origin) whose credit
  is still open (the invoice was already paid: prepaid, COD or at the counter) is refunded: a refund row
  ``PENDING_APPROVAL`` when the credit is issued, approved by Finance 0–3 days later (some are still pending at the
  end of the history, a master-prompt scenario), processed the same day: to the original card / UPI / net-banking
  payment, in cash from the store's float for a counter sale paid in cash, or by bank transfer (NEFT) for COD.
  ~2% fail and are retried the next working day as a new refund. COMPLETED → **P14** Dr AR / Cr Bank, an AR refund
  item settled against the credit note (allocation credit note → refund, D49). Business customers' credits stay on
  account and are applied to their next invoices (collections, 3.10).
* **Prepaid orders cancelled before invoicing.** There is no invoice and so no credit note (refunds pay out credit
  notes, R5): the gateway payment is refunded to its source as a **reversal of the payment** (``REVERSED``, reason
  "Refunded: order cancelled", P09 reverses its P08, the AR item is REVERSED), 1–5 days after the cancellation.
  Small overpayments of invoiced prepaid orders (a GST cut between order and invoice, cancelled lines, a split
  order's shipping GST) stay as open customer credits: v1 has no refund document for unapplied cash.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal, reverse_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver
from scmgen.generation.engine.fulfilment.invoicing import ar_state
from scmgen.generation.numbering import Registration

FAILURES = ("Beneficiary account closed", "Card network declined the refund", "Bank timeout")
TO_SOURCE = ("UPI", "CARD", "NET_BANKING")


class _Refunds:
    def __init__(self, day: DayContext) -> None:
        self.day = day
        self.book = account_book(day)
        self.methods = {r["method_code"]: r["payment_method_id"] for r in day.rows(
            "SELECT payment_method_id, method_code FROM payment_methods")}
        accounts = {r["account_purpose"]: r["bank_account_id"] for r in day.rows(
            "SELECT account_purpose, MIN(bank_account_id) AS bank_account_id FROM bank_accounts "
            "WHERE account_status = 'ACTIVE' AND account_purpose IN ('COLLECTIONS', 'PAYMENTS') GROUP BY 1")}
        self.collections = accounts["COLLECTIONS"]
        self.payments = accounts.get("PAYMENTS", self.collections)
        self.floats = {r["warehouse_id"]: r["bank_account_id"] for r in day.rows(
            "SELECT warehouse_id, bank_account_id FROM bank_accounts WHERE account_type = 'PETTY_CASH'")}
        principal = day.rows("""SELECT r.company_gst_registration_id, s.state_code FROM company_gst_registrations r
                                JOIN states s ON s.state_id = r.state_id WHERE r.is_principal_place""")[0]
        self.registration = Registration(principal["company_gst_registration_id"], principal["state_code"])


def refunds_step(day: DayContext) -> None:
    resources = _Refunds(day)
    _request(day, resources)
    if day.is_working_day:
        _process(day, resources)
    _refund_cancelled_prepayments(day)


def _request(day: DayContext, resources: _Refunds) -> None:
    """A refund for every consumer credit note still open and not being refunded (a failed refund is retried)."""
    for note in day.rows("""
            SELECT n.credit_note_id, n.customer_id, n.invoice_id, a.open_amount, a.updated_at, i.order_id,
                   o.store_warehouse_id,
                   (SELECT p.customer_payment_id FROM customer_payment_allocations x
                     JOIN customer_payments p ON p.customer_payment_id = x.customer_payment_id
                    WHERE x.invoice_id = n.invoice_id AND p.payment_status = 'SUCCESS'
                    ORDER BY p.customer_payment_id LIMIT 1) AS paid_by
            FROM credit_notes n JOIN accounts_receivable a ON a.credit_note_id = n.credit_note_id
            JOIN customers c ON c.customer_id = n.customer_id JOIN invoices i ON i.invoice_id = n.invoice_id
            JOIN orders o ON o.order_id = i.order_id
            WHERE c.customer_type = 'B2C' AND a.open_amount < 0 AND n.credit_note_status = 'ISSUED'
              AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.credit_note_id = n.credit_note_id
                                AND r.refund_status NOT IN ('FAILED', 'REJECTED'))
            ORDER BY n.credit_note_id"""):
        if note["paid_by"] is None:
            continue
        payment = day.rows("""SELECT p.customer_payment_id, p.payment_type, m.method_code FROM customer_payments p
                              JOIN payment_methods m USING (payment_method_id) WHERE p.customer_payment_id = :p""",
                           p=note["paid_by"])[0]
        method = payment["method_code"]
        if method in TO_SOURCE:
            source, bank, code = payment["customer_payment_id"], resources.collections, method
        elif method == "CASH" and payment["payment_type"] == "COUNTER_PAYMENT" and note["store_warehouse_id"]:
            source, bank, code = None, resources.floats[note["store_warehouse_id"]], "CASH"
        else:                                          # COD and other cash: bank transfer to the customer
            source, bank, code = None, resources.payments, "NEFT"
        requested = max(note["updated_at"], day.at(9)) + timedelta(minutes=5)
        if requested.date() != day.day:
            requested = day.at(9)
        day.writer.insert("refunds", {
            "refund_id": day.ids.next("refunds"),
            "refund_number": day.numbers.next(day.conn, "RFD", day.day, resources.registration),
            "credit_note_id": note["credit_note_id"], "customer_payment_id": source, "refund_amount": -note["open_amount"],
            "payment_method_id": resources.methods[code], "bank_account_id": bank, "requested_at": requested,
            "approved_by_employee_id": None, "approved_at": None, "processed_at": None, "bank_reference": None,
            "refund_status": "PENDING_APPROVAL", "failure_reason": None, "created_at": requested, "updated_at": requested,
        })


def _process(day: DayContext, resources: _Refunds) -> None:
    """Finance approves pending refunds after 0–3 days; the bank processes them the same day (P14)."""
    settings = day.world.config.returns
    for refund in day.rows("""
            SELECT r.refund_id, r.refund_number, r.credit_note_id, r.refund_amount, r.bank_account_id, r.requested_at,
                   n.customer_id, a.accounts_receivable_id, a.original_amount, a.open_amount
            FROM refunds r JOIN credit_notes n ON n.credit_note_id = r.credit_note_id
            JOIN accounts_receivable a ON a.credit_note_id = n.credit_note_id
            WHERE r.refund_status = 'PENDING_APPROVAL' ORDER BY r.refund_id"""):
        rng = day.streams.stream(refund["requested_at"].date(), f"refunds.{refund['refund_id']}")
        if day.day < refund["requested_at"].date() + timedelta(days=rng.randint(*settings.refund_approval_days)):
            continue
        approved = max(day.at(rng.randint(10, 15), rng.randint(0, 59)), refund["requested_at"] + timedelta(minutes=10))
        processed = approved + timedelta(minutes=rng.randint(20, 180))
        by = approver(day, refund["refund_amount"], ("FIN",), day.day)
        if rng.random() < settings.refund_failure_share:
            day.writer.update("refunds", "refund_id", refund["refund_id"], {
                "approved_by_employee_id": by, "approved_at": approved, "processed_at": processed, "bank_reference": None,
                "refund_status": "FAILED", "failure_reason": rng.choice(FAILURES), "updated_at": processed})
            continue
        day.writer.update("refunds", "refund_id", refund["refund_id"], {
            "approved_by_employee_id": by, "approved_at": approved, "processed_at": processed,
            "bank_reference": f"XRFD{day.day:%y%m%d}{refund['refund_id']:08d}", "refund_status": "COMPLETED",
            "failure_reason": None, "updated_at": processed})
        amount = refund["refund_amount"]
        journal = (JournalBuilder(f"Refund {refund['refund_number']}")
                   .debit(resources.book.role("AR_CONTROL"), amount, customer_id=refund["customer_id"])
                   .credit(resources.book.bank(refund["bank_account_id"]), amount).build())
        journal_id = post_journal(day, journal, source="REFUND", refund_id=refund["refund_id"])
        day.writer.insert("accounts_receivable", {
            "accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": refund["customer_id"],
            "item_type": "REFUND", "invoice_id": None, "credit_note_id": None, "customer_payment_id": None,
            "refund_id": refund["refund_id"], "document_date": day.day, "due_date": day.day, "original_amount": amount,
            "open_amount": Decimal(0), "item_status": "SETTLED", "settled_at": processed, "journal_entry_id": journal_id,
            "created_at": processed, "updated_at": processed,
        })
        day.writer.insert("customer_payment_allocations", {
            "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
            "customer_payment_id": None, "credit_note_id": refund["credit_note_id"], "invoice_id": None,
            "refund_id": refund["refund_id"], "allocated_amount": amount, "allocation_date": day.day,
            "allocated_by_employee_id": None, "allocation_status": "ACTIVE", "reversed_at": None,
            "created_at": processed, "updated_at": processed,
        })
        day.writer.update("accounts_receivable", "accounts_receivable_id", refund["accounts_receivable_id"],
                          ar_state(refund["original_amount"], refund["open_amount"] + amount, processed))


def _refund_cancelled_prepayments(day: DayContext) -> None:
    """Prepayments of orders cancelled before invoicing go back to their source (P09 reversal of the receipt)."""
    for payment in day.rows("""
            SELECT p.customer_payment_id, o.cancelled_at, j.journal_entry_id, a.accounts_receivable_id
            FROM customer_payments p JOIN orders o ON o.order_id = p.order_id
            JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
            JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id AND j.journal_source = 'CUSTOMER_PAYMENT'
            WHERE p.payment_type = 'ORDER_PREPAYMENT' AND p.payment_status = 'SUCCESS' AND o.order_status = 'CANCELLED'
              AND a.item_status = 'OPEN' AND a.open_amount = a.original_amount AND o.cancelled_at < :today
            ORDER BY p.customer_payment_id""", today=day.at(0)):
        rng = day.streams.stream(payment["cancelled_at"].date(), f"refunds.cancelled.{payment['customer_payment_id']}")
        due = payment["cancelled_at"].date() + timedelta(days=rng.randint(*day.world.config.returns.cancelled_refund_days))
        if day.day < due:
            continue
        at = day.at(rng.randint(10, 17), rng.randint(0, 59))
        reverse_journal(day, payment["journal_entry_id"], f"Refund of payment {payment['customer_payment_id']}")
        day.writer.update("customer_payments", "customer_payment_id", payment["customer_payment_id"], {
            "payment_status": "REVERSED", "reversed_at": at, "reversal_reason": "Refunded: order cancelled",
            "updated_at": at})
        day.writer.update("accounts_receivable", "accounts_receivable_id", payment["accounts_receivable_id"], {
            "open_amount": Decimal(0), "item_status": "REVERSED", "settled_at": None, "updated_at": at})


