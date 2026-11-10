"""Accounts receivable rules (1E §7, V-AR-*). Each check returns the violating rows.

Step 3.8a brings the first AR items (successful prepayments); the checks already cover every item type, so
invoices (3.9), credit notes and refunds (3.11) are checked as soon as they exist.
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# AR item type → (source column, documents (alias d) with their customer, signed original amount, item exists when).
# A refund's customer is its credit note's.
SOURCES = {
    "INVOICE": ("invoice_id", "invoices d", "d.customer_id", "d.total_amount", "d.invoice_status = 'ISSUED'"),
    "CREDIT_NOTE": ("credit_note_id", "credit_notes d", "d.customer_id", "-d.total_amount",
                    "d.credit_note_status <> 'CANCELLED'"),
    "PAYMENT": ("customer_payment_id", "customer_payments d", "d.customer_id", "-d.amount",
                "d.payment_status IN ('SUCCESS', 'REVERSED')"),
    "REFUND": ("refund_id", "refunds d JOIN credit_notes n ON n.credit_note_id = d.credit_note_id", "n.customer_id",
               "d.refund_amount", "d.refund_status = 'COMPLETED'"),
}
# AR item type → allocation column whose ACTIVE allocations reduce its open amount.
ALLOCATED_BY = {"INVOICE": "invoice_id", "CREDIT_NOTE": "credit_note_id", "PAYMENT": "customer_payment_id",
                "REFUND": "refund_id"}


@check("V-AR-01")
def subledger_equals_gl_per_customer(ctx: ValidationContext) -> Rows:
    """Σ open AR (excluding reversed items) per customer = the customer's balance on the AR control account."""
    return ctx.rows("""
        SELECT k.customer_id, COALESCE(s.open_amount, 0) AS subledger, COALESCE(g.balance, 0) AS gl
        FROM (SELECT customer_id FROM accounts_receivable
              UNION SELECT l.customer_id FROM journal_entry_lines l
                    JOIN chart_of_accounts a ON a.account_id = l.account_id WHERE a.control_subledger = 'AR') k
        LEFT JOIN (SELECT customer_id, SUM(open_amount) AS open_amount FROM accounts_receivable
                   WHERE item_status <> 'REVERSED' GROUP BY customer_id) s ON s.customer_id = k.customer_id
        LEFT JOIN (SELECT l.customer_id, SUM(l.debit_amount - l.credit_amount) AS balance FROM journal_entry_lines l
                   JOIN chart_of_accounts a ON a.account_id = l.account_id
                   WHERE a.control_subledger = 'AR' GROUP BY l.customer_id) g ON g.customer_id = k.customer_id
        WHERE COALESCE(s.open_amount, 0) <> COALESCE(g.balance, 0)""")


@check("V-AR-02")
def items_follow_their_documents(ctx: ValidationContext) -> Rows:
    """Exactly one AR item per ISSUED invoice, non-cancelled credit note, SUCCESS (or later REVERSED) payment and
    COMPLETED refund, for the document's customer and with the document's total (signed); none for others."""
    found: Rows = []
    for item_type, (column, documents, customer, amount, exists) in SOURCES.items():
        found += [{**r, "item_type": item_type, "problem": "AR item missing, extra or with another amount/customer"}
                  for r in ctx.rows(f"""
            SELECT d.{column} AS document_id, a.accounts_receivable_id, {amount} AS expected, a.original_amount
            FROM {documents} LEFT JOIN accounts_receivable a ON a.{column} = d.{column}
            WHERE ({exists}) <> (a.accounts_receivable_id IS NOT NULL)
               OR a.original_amount <> {amount} OR a.customer_id <> {customer} OR a.item_type <> '{item_type}'""")]
    return found


@check("V-AR-03")
def open_amounts_follow_allocations(ctx: ValidationContext) -> Rows:
    """Open amount = original − Σ ACTIVE allocations on the item (signed); allocations join items of the same
    customer, use only SUCCESS payments and are dated on or after both documents (1E)."""
    found: Rows = []
    for item_type, column in ALLOCATED_BY.items():
        found += [{**r, "item_type": item_type, "problem": "open ≠ original − Σ active allocations"} for r in ctx.rows(f"""
            SELECT a.accounts_receivable_id, a.original_amount, a.open_amount, COALESCE(x.allocated, 0) AS allocated
            FROM accounts_receivable a
            LEFT JOIN (SELECT {column}, SUM(allocated_amount) AS allocated FROM customer_payment_allocations
                       WHERE allocation_status = 'ACTIVE' AND {column} IS NOT NULL GROUP BY {column}) x
                   ON x.{column} = a.{column}
            WHERE a.item_type = '{item_type}' AND a.item_status <> 'REVERSED'
              AND a.open_amount <> a.original_amount - SIGN(a.original_amount) * COALESCE(x.allocated, 0)""")]
    found += [{**r, "problem": "allocation joins two customers, a payment that isn't SUCCESS or is dated too early"}
              for r in ctx.rows("""
        SELECT x.customer_payment_allocation_id, x.allocation_date FROM customer_payment_allocations x
        LEFT JOIN customer_payments p ON p.customer_payment_id = x.customer_payment_id
        LEFT JOIN credit_notes c ON c.credit_note_id = x.credit_note_id
        LEFT JOIN invoices i ON i.invoice_id = x.invoice_id
        LEFT JOIN refunds r ON r.refund_id = x.refund_id
        LEFT JOIN credit_notes rc ON rc.credit_note_id = r.credit_note_id
        WHERE (p.customer_payment_id IS NOT NULL AND p.payment_status <> 'SUCCESS' AND x.allocation_status = 'ACTIVE')
           OR COALESCE(p.customer_id, c.customer_id) <> COALESCE(i.customer_id, rc.customer_id)
           OR x.allocation_date < COALESCE(p.payment_date, c.credit_note_date)
           OR x.allocation_date < COALESCE(i.invoice_date, DATE(r.processed_at), x.allocation_date)""")]
    return found
