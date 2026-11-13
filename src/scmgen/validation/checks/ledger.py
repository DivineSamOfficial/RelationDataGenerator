"""Ledger completeness and period balances (1E §7 V-AC-12, V-AC-13; step 3.14c). Each check returns the
violating rows."""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# V-AC-12: journal source → (source FK column, the events that must have exactly one journal of that source).
EVENT_JOURNALS = [
    ("GOODS_RECEIPT", "goods_receipt_id", """
        SELECT g.goods_receipt_id AS id FROM goods_receipts g WHERE g.grn_status = 'INSPECTED'
          AND EXISTS (SELECT 1 FROM goods_receipt_line_items l JOIN goods_inspections i
                        ON i.goods_receipt_line_item_id = l.goods_receipt_line_item_id
                      WHERE l.goods_receipt_id = g.goods_receipt_id AND i.quantity_accepted > 0)"""),
    ("SUPPLIER_INVOICE", "supplier_invoice_id", "SELECT supplier_invoice_id AS id FROM supplier_invoices WHERE invoice_status = 'APPROVED'"),
    ("SUPPLIER_PAYMENT", "supplier_payment_id", "SELECT supplier_payment_id AS id FROM supplier_payments WHERE payment_status = 'COMPLETED'"),
    ("SALES_INVOICE", "invoice_id", "SELECT invoice_id AS id FROM invoices"),
    ("CUSTOMER_PAYMENT", "customer_payment_id",
     "SELECT customer_payment_id AS id FROM customer_payments WHERE payment_status IN ('SUCCESS', 'REVERSED')"),
    ("CREDIT_NOTE", "credit_note_id", "SELECT credit_note_id AS id FROM credit_notes WHERE credit_note_status = 'ISSUED'"),
    ("SALES_RETURN_RECEIPT", "sales_return_id", "SELECT sales_return_id AS id FROM sales_returns WHERE received_at IS NOT NULL"),
    ("RTO_RECEIPT", "shipment_id", "SELECT shipment_id AS id FROM shipments WHERE shipment_status = 'RETURNED_TO_SENDER'"),
    ("REFUND", "refund_id", "SELECT refund_id AS id FROM refunds WHERE refund_status = 'COMPLETED'"),
    ("STOCK_ADJUSTMENT", "stock_adjustment_id", """
        SELECT stock_adjustment_id AS id FROM stock_adjustment_line_items GROUP BY stock_adjustment_id
        HAVING SUM(ABS(value_change)) > 0"""),
    ("TRANSFER_DISPATCH", "stock_transfer_id", "SELECT stock_transfer_id AS id FROM stock_transfers WHERE dispatched_at IS NOT NULL"),
    ("TRANSFER_RECEIPT", "stock_transfer_id", "SELECT stock_transfer_id AS id FROM stock_transfers WHERE transfer_status = 'RECEIVED'"),
    ("DEFECTIVE_DISPOSITION", "defective_stock_id", """
        SELECT d.defective_stock_id AS id FROM defective_stock d JOIN inventory_transactions t
             ON t.defective_stock_id = d.defective_stock_id
        WHERE d.disposition IN ('SCRAP', 'RETURN_TO_SUPPLIER') GROUP BY d.defective_stock_id HAVING SUM(t.value_change) <> 0"""),
    ("EXPENSE", "expense_id", "SELECT expense_id AS id FROM expenses WHERE expense_status = 'APPROVED'"),
    ("EXPENSE_PAYMENT", "expense_payment_id", "SELECT expense_payment_id AS id FROM expense_payments WHERE payment_status = 'COMPLETED'"),
    ("BANK_SETTLEMENT", "bank_transaction_id", """
        SELECT t.bank_transaction_id AS id FROM bank_transactions t
        LEFT JOIN customer_payments p ON p.customer_payment_id = t.customer_payment_id
        WHERE t.transaction_type IN ('GATEWAY_SETTLEMENT', 'COD_REMITTANCE')
           OR (t.transaction_type = 'REVERSAL' AND p.payment_status = 'REVERSED')"""),
    ("BANK_CHARGES", "bank_transaction_id", "SELECT bank_transaction_id AS id FROM bank_transactions WHERE transaction_type = 'BANK_CHARGES'"),
    ("INTEREST", "bank_transaction_id", "SELECT bank_transaction_id AS id FROM bank_transactions WHERE transaction_type = 'INTEREST_CREDIT'"),
    ("INTERNAL_TRANSFER", "bank_transaction_id", """
        SELECT bank_transaction_id AS id FROM bank_transactions WHERE transaction_type = 'INTERNAL_TRANSFER' AND debit_amount > 0"""),
    ("GST_SETTLEMENT", "gst_return_id", """
        SELECT gst_return_id AS id FROM gst_returns WHERE return_type = 'GSTR3B' AND return_status <> 'DRAFT'
          AND output_cgst + output_sgst + output_igst > 0"""),
]


@check("V-AC-12")
def every_event_has_its_journal(ctx: ValidationContext) -> Rows:
    """★ Every financially relevant event has exactly one journal of its source (§6), every such journal has its
    event, a REVERSAL journal mirrors a REVERSED original, and every month-end in the history has its depreciation
    (while assets remain to depreciate)."""
    found: Rows = []
    for source, column, events in EVENT_JOURNALS:
        found += [{**r, "journal_source": source} for r in ctx.rows(f"""
            SELECT e.id, COUNT(j.journal_entry_id) AS journals, 'event without exactly one journal' AS problem
            FROM ({events}) e LEFT JOIN journal_entries j ON j.{column} = e.id AND j.journal_source = '{source}'
            GROUP BY e.id HAVING journals <> 1
            UNION ALL
            SELECT j.{column}, 1, 'journal without its event' FROM journal_entries j
            WHERE j.journal_source = '{source}' AND (j.{column} IS NULL OR j.{column} NOT IN (SELECT id FROM ({events}) e2))""")]
    found += [{**r, "problem": "reversal of a journal that isn't REVERSED, or a REVERSED journal without its reversal"}
              for r in ctx.rows("""
        SELECT j.journal_entry_id, j.entry_status, r.journal_entry_id AS reversal FROM journal_entries j
        LEFT JOIN journal_entries r ON r.reverses_journal_entry_id = j.journal_entry_id
        WHERE (j.entry_status = 'REVERSED') <> (r.journal_entry_id IS NOT NULL)""")]
    found += [{**r, "problem": "month end without its depreciation"} for r in ctx.rows("""
        SELECT p.period_code FROM accounting_periods p
        WHERE p.end_date BETWEEN (SELECT MIN(sim_start_date) FROM generation_runs WHERE sim_start_date < sim_end_date)
                             AND (SELECT DATE(MAX(committed_through)) FROM generation_runs)
          AND EXISTS (SELECT 1 FROM journal_entries o WHERE o.journal_source = 'OPENING_BALANCE')
          AND NOT EXISTS (SELECT 1 FROM journal_entries j WHERE j.journal_source = 'DEPRECIATION'
                            AND j.document_date = p.end_date)
          AND (SELECT COALESCE(SUM(l.credit_amount - l.debit_amount), 0) FROM journal_entry_lines l
               JOIN chart_of_accounts a ON a.account_id = l.account_id WHERE a.system_role = 'ACCUMULATED_DEPRECIATION')
              < (SELECT COALESCE(SUM(l.debit_amount - l.credit_amount), 0) FROM journal_entry_lines l
                 JOIN chart_of_accounts a ON a.account_id = l.account_id WHERE a.account_code IN ('15100', '15200'))""")]
    return found


@check("V-AC-13")
def period_balances_reaggregate(ctx: ValidationContext) -> Rows:
    """★ ``gl_period_balances`` of every CLOSED period = re-aggregation of the journal lines posted into it, one row
    per postable account, opening = the previous closing (P&L from zero each year, Retained Earnings absorbing
    the previous year, D54); the trial balance of every period nets to zero."""
    found = [{**r, "problem": "closed period without a balance row per postable account"} for r in ctx.rows("""
        SELECT p.period_code, COUNT(b.gl_period_balance_id) AS rows_,
               (SELECT COUNT(*) FROM chart_of_accounts WHERE is_postable) AS accounts
        FROM accounting_periods p LEFT JOIN gl_period_balances b ON b.accounting_period_id = p.accounting_period_id
        WHERE p.period_status = 'CLOSED' AND EXISTS (SELECT 1 FROM journal_entries j
                                                     WHERE j.accounting_period_id = p.accounting_period_id)
        GROUP BY p.accounting_period_id, p.period_code HAVING rows_ <> accounts""")]
    found += [{**r, "problem": "period movement ≠ Σ journal lines of the period"} for r in ctx.rows("""
        SELECT b.account_id, b.accounting_period_id, b.period_debit, b.period_credit, m.debit, m.credit
        FROM gl_period_balances b
        LEFT JOIN (SELECT l.account_id, j.accounting_period_id, SUM(l.debit_amount) AS debit, SUM(l.credit_amount) AS credit
                   FROM journal_entry_lines l JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                   GROUP BY 1, 2) m ON m.account_id = b.account_id AND m.accounting_period_id = b.accounting_period_id
        WHERE b.period_debit <> COALESCE(m.debit, 0) OR b.period_credit <> COALESCE(m.credit, 0)""")]
    found += [{**r, "problem": "opening ≠ the previous period's closing (P&L reset and Retained Earnings rule, D54)"}
              for r in ctx.rows("""
        SELECT b.account_id, p.period_code, b.opening_balance, pb.closing_balance AS previous_closing,
               t.financial_statement, a.system_role, (pp.fiscal_year <> p.fiscal_year) AS new_year
        FROM gl_period_balances b JOIN accounting_periods p ON p.accounting_period_id = b.accounting_period_id
        JOIN chart_of_accounts a ON a.account_id = b.account_id JOIN account_types t ON t.account_type_id = a.account_type_id
        JOIN accounting_periods pp ON pp.end_date = p.start_date - INTERVAL 1 DAY
        JOIN gl_period_balances pb ON pb.accounting_period_id = pp.accounting_period_id AND pb.account_id = b.account_id
        WHERE (t.financial_statement = 'PROFIT_AND_LOSS' AND b.opening_balance <> IF(pp.fiscal_year <> p.fiscal_year, 0, pb.closing_balance))
           OR (t.financial_statement = 'BALANCE_SHEET' AND COALESCE(a.system_role, '') <> 'RETAINED_EARNINGS'
               AND b.opening_balance <> pb.closing_balance)
           OR (a.system_role = 'RETAINED_EARNINGS' AND pp.fiscal_year = p.fiscal_year AND b.opening_balance <> pb.closing_balance)""")]
    found += [{**r, "problem": "trial balance doesn't net to zero"} for r in ctx.rows("""
        SELECT accounting_period_id, SUM(closing_balance) AS net, SUM(opening_balance) AS opening_net
        FROM gl_period_balances GROUP BY accounting_period_id
        HAVING SUM(closing_balance) <> 0 OR SUM(opening_balance) <> 0""")]
    return found
