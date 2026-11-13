"""Bank rules (1E §7 V-BK-*; step 3.14a). Each check returns the violating rows."""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# Document → the statement line it must have: (type, link column, the documents with their account and amount).
DOCUMENT_LINES = [
    ("SUPPLIER_PAYMENT", "supplier_payment_id", """
        SELECT supplier_payment_id AS id, bank_account_id, amount FROM supplier_payments WHERE payment_status = 'COMPLETED'"""),
    ("EXPENSE_PAYMENT", "expense_payment_id", """
        SELECT expense_payment_id AS id, bank_account_id, amount FROM expense_payments WHERE payment_status = 'COMPLETED'"""),
    ("REFUND", "refund_id", """
        SELECT refund_id AS id, bank_account_id, refund_amount AS amount FROM refunds WHERE refund_status = 'COMPLETED'"""),
    ("GST_PAYMENT", "gst_return_id", """
        SELECT gst_return_id AS id, bank_account_id, cash_paid_cgst + cash_paid_sgst + cash_paid_igst AS amount
        FROM gst_returns WHERE payment_date IS NOT NULL"""),
    ("CUSTOMER_RECEIPT", "customer_payment_id", """
        SELECT p.customer_payment_id AS id, p.bank_account_id, p.amount FROM customer_payments p
        JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
        WHERE p.payment_status IN ('SUCCESS', 'REVERSED') AND p.payment_type <> 'COD_COLLECTION'
          AND m.method_code NOT IN ('UPI', 'CARD', 'NET_BANKING', 'CHEQUE')"""),
    ("CHEQUE_DEPOSIT", "customer_payment_id", """
        SELECT p.customer_payment_id AS id, p.bank_account_id, p.amount FROM customer_payments p
        JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
        WHERE m.method_code = 'CHEQUE' AND (p.payment_status IN ('SUCCESS', 'REVERSED')
                                           OR (p.payment_status = 'FAILED' AND p.completed_at IS NOT NULL))"""),
]


@check("V-BK-01")
def bank_lines(ctx: ValidationContext) -> Rows:
    """★ Every completed payment, refund and direct receipt has exactly one statement line of its type on its account
    with its amount (and no line lacks its document); settlement lines = Σ their settled receipts − fees; each
    internal transfer has its paired leg of the same amount on the other side; ``balance_after`` chains per account
    in (statement date, id) order from the account's opening balance."""
    found: Rows = []
    for kind, column, documents in DOCUMENT_LINES:
        amount_column = ("debit_amount" if kind in ("SUPPLIER_PAYMENT", "EXPENSE_PAYMENT", "REFUND", "GST_PAYMENT")
                         else "credit_amount")
        found += [{**r, "transaction_type": kind} for r in ctx.rows(f"""
            SELECT d.id, d.amount, COUNT(t.bank_transaction_id) AS lines_, MIN(t.{amount_column}) AS line_amount,
                   'document without exactly one statement line of its amount and account' AS problem
            FROM ({documents}) d
            LEFT JOIN bank_transactions t ON t.{column} = d.id AND t.transaction_type = '{kind}'
            GROUP BY d.id, d.amount, d.bank_account_id
            HAVING lines_ <> 1 OR MIN(t.{amount_column}) <> d.amount OR MIN(t.bank_account_id) <> d.bank_account_id
            UNION ALL
            SELECT t.{column}, NULL, 1, t.{amount_column}, 'statement line without its document' FROM bank_transactions t
            WHERE t.transaction_type = '{kind}' AND t.{column} NOT IN (SELECT id FROM ({documents}) d2)""")]
    found += [{**r, "problem": "settlement line ≠ Σ settled receipts − fees"} for r in ctx.rows("""
        SELECT t.bank_transaction_id, t.credit_amount, COALESCE(SUM(p.amount - p.gateway_fee_amount), 0) AS settled
        FROM bank_transactions t LEFT JOIN customer_payments p ON p.settlement_bank_transaction_id = t.bank_transaction_id
        WHERE t.transaction_type IN ('GATEWAY_SETTLEMENT', 'COD_REMITTANCE')
        GROUP BY t.bank_transaction_id, t.credit_amount
        HAVING t.credit_amount <> settled""")]
    found += [{**r, "problem": "internal transfer without its paired leg"} for r in ctx.rows("""
        SELECT t.bank_transaction_id, t.counter_bank_transaction_id FROM bank_transactions t
        LEFT JOIN bank_transactions c ON c.bank_transaction_id = t.counter_bank_transaction_id
        WHERE t.transaction_type = 'INTERNAL_TRANSFER'
          AND (c.bank_transaction_id IS NULL OR c.counter_bank_transaction_id <> t.bank_transaction_id
               OR c.transaction_type <> 'INTERNAL_TRANSFER' OR c.credit_amount <> t.debit_amount
               OR c.debit_amount <> t.credit_amount OR c.bank_account_id = t.bank_account_id)""")]
    found += [{**r, "problem": "balance_after doesn't chain from the opening balance"} for r in ctx.rows("""
        SELECT x.bank_transaction_id, x.bank_account_id, x.balance_after, x.expected FROM (
            SELECT t.bank_transaction_id, t.bank_account_id, t.balance_after,
                   COALESCE(o.opening, 0) + SUM(t.credit_amount - t.debit_amount)
                       OVER (PARTITION BY t.bank_account_id ORDER BY t.statement_date, t.bank_transaction_id) AS expected
            FROM bank_transactions t JOIN bank_accounts b ON b.bank_account_id = t.bank_account_id
            LEFT JOIN (SELECT l.account_id, SUM(l.debit_amount - l.credit_amount) AS opening FROM journal_entry_lines l
                       JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                       WHERE j.journal_source = 'OPENING_BALANCE' GROUP BY l.account_id) o ON o.account_id = b.gl_account_id) x
        WHERE x.balance_after <> x.expected""")]
    return found


@check("V-BK-02")
def monthly_reconciliations(ctx: ValidationContext) -> Rows:
    """★ Every account has a RECONCILED reconciliation with a zero difference for every month whose
    reconciliation was due (the books' and the statement's closing balances agree)."""
    return ctx.rows("""
        SELECT b.bank_account_id, p.period_code, r.reconciliation_status, r.difference_amount
        FROM bank_accounts b CROSS JOIN accounting_periods p
        LEFT JOIN bank_reconciliations r ON r.bank_account_id = b.bank_account_id
             AND r.accounting_period_id = p.accounting_period_id
        WHERE b.account_status = 'ACTIVE'
          AND p.start_date >= (SELECT DATE_FORMAT(MIN(statement_date), '%Y-%m-01') FROM bank_transactions)
          AND p.end_date < (SELECT MAX(sim_end_date) FROM generation_runs) - INTERVAL 10 DAY
          AND EXISTS (SELECT 1 FROM bank_transactions t WHERE t.statement_date > p.end_date + INTERVAL 10 DAY)
          AND (r.bank_reconciliation_id IS NULL OR r.reconciliation_status <> 'RECONCILED' OR r.difference_amount <> 0)""")
