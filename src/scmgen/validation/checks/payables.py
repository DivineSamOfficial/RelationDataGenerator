"""Accounts payable rules (1E §7, V-AP-*). Each check returns the violating rows.

Step 3.7c implements both for supplier invoices and payments; expenses (step 3.13) join the same checks.
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-AP-01")
def subledger_equals_gl_per_payee(ctx: ValidationContext) -> Rows:
    """Σ AP open amounts per payee = the payee's balance on the AP control account (credit − debit)."""
    return ctx.rows("""
        SELECT k.supplier_id, k.employee_id, COALESCE(s.open_amount, 0) AS subledger, COALESCE(g.balance, 0) AS gl
        FROM (SELECT supplier_id, employee_id FROM accounts_payable
              UNION SELECT l.supplier_id, l.employee_id FROM journal_entry_lines l
                    JOIN chart_of_accounts a ON a.account_id = l.account_id WHERE a.control_subledger = 'AP') k
        LEFT JOIN (SELECT supplier_id, employee_id, SUM(open_amount) AS open_amount FROM accounts_payable
                   GROUP BY supplier_id, employee_id) s
               ON s.supplier_id <=> k.supplier_id AND s.employee_id <=> k.employee_id
        LEFT JOIN (SELECT l.supplier_id, l.employee_id, SUM(l.credit_amount - l.debit_amount) AS balance
                   FROM journal_entry_lines l JOIN chart_of_accounts a ON a.account_id = l.account_id
                   WHERE a.control_subledger = 'AP' GROUP BY l.supplier_id, l.employee_id) g
               ON g.supplier_id <=> k.supplier_id AND g.employee_id <=> k.employee_id
        WHERE COALESCE(s.open_amount, 0) <> COALESCE(g.balance, 0)""")


@check("V-AP-02")
def items_follow_their_documents(ctx: ValidationContext) -> Rows:
    """One AP item per APPROVED supplier invoice (+ total), per APPROVED non-payroll expense (D56/D57) and per
    COMPLETED supplier payment (− amount), none for other statuses; an expense item's open = total − Σ COMPLETED
    expense payments; open amounts = original − Σ ACTIVE allocations; allocations link a COMPLETED payment
    and an approved invoice of the same supplier, within both amounts."""
    found = [{**r, "problem": "payment AP item missing, extra or with another amount"} for r in ctx.rows("""
        SELECT p.supplier_payment_id, p.payment_status, p.amount, a.original_amount FROM supplier_payments p
        LEFT JOIN accounts_payable a ON a.supplier_payment_id = p.supplier_payment_id
        WHERE (p.payment_status = 'COMPLETED') <> (a.accounts_payable_id IS NOT NULL) OR a.original_amount <> -p.amount""")]
    found += [{**r, "problem": "expense AP item missing, extra, for payroll or with another amount"} for r in ctx.rows("""
        SELECT e.expense_id, e.expense_type, e.expense_status, e.total_amount, a.original_amount FROM expenses e
        LEFT JOIN accounts_payable a ON a.expense_id = e.expense_id
        WHERE (e.expense_status = 'APPROVED' AND e.expense_type <> 'PAYROLL') <> (a.accounts_payable_id IS NOT NULL)
           OR a.original_amount <> e.total_amount""")]
    found += [{**r, "problem": "expense item open ≠ total − Σ completed payments"} for r in ctx.rows("""
        SELECT a.accounts_payable_id, a.original_amount, a.open_amount, COALESCE(p.paid, 0) AS paid
        FROM accounts_payable a
        LEFT JOIN (SELECT expense_id, SUM(amount) AS paid FROM expense_payments WHERE payment_status = 'COMPLETED'
                   GROUP BY expense_id) p ON p.expense_id = a.expense_id
        WHERE a.item_type = 'EXPENSE' AND a.open_amount <> a.original_amount - COALESCE(p.paid, 0)""")]
    found += [{**r, "problem": "invoice item open ≠ total − Σ active allocations"} for r in ctx.rows("""
        SELECT a.accounts_payable_id, a.original_amount, a.open_amount, COALESCE(x.allocated, 0) AS allocated
        FROM accounts_payable a
        LEFT JOIN (SELECT supplier_invoice_id, SUM(allocated_amount) AS allocated FROM supplier_payment_allocations
                   WHERE allocation_status = 'ACTIVE' GROUP BY supplier_invoice_id) x ON x.supplier_invoice_id = a.supplier_invoice_id
        WHERE a.item_type = 'SUPPLIER_INVOICE' AND a.open_amount <> a.original_amount - COALESCE(x.allocated, 0)""")]
    found += [{**r, "problem": "payment item open ≠ −(amount − Σ active allocations)"} for r in ctx.rows("""
        SELECT a.accounts_payable_id, a.original_amount, a.open_amount, COALESCE(x.allocated, 0) AS allocated
        FROM accounts_payable a
        LEFT JOIN (SELECT supplier_payment_id, SUM(allocated_amount) AS allocated FROM supplier_payment_allocations
                   WHERE allocation_status = 'ACTIVE' GROUP BY supplier_payment_id) x ON x.supplier_payment_id = a.supplier_payment_id
        WHERE a.item_type = 'SUPPLIER_PAYMENT' AND a.open_amount <> a.original_amount + COALESCE(x.allocated, 0)""")]
    found += [{**r, "problem": "allocation links another supplier, a non-completed payment or an unapproved invoice"}
              for r in ctx.rows("""
        SELECT x.supplier_payment_allocation_id FROM supplier_payment_allocations x
        JOIN supplier_payments p ON p.supplier_payment_id = x.supplier_payment_id
        JOIN supplier_invoices i ON i.supplier_invoice_id = x.supplier_invoice_id
        WHERE p.supplier_id <> i.supplier_id OR p.payment_status <> 'COMPLETED' OR i.invoice_status <> 'APPROVED'""")]
    found += [{**r, "problem": "allocated more than the payment"} for r in ctx.rows("""
        SELECT p.supplier_payment_id, p.amount, SUM(x.allocated_amount) AS allocated FROM supplier_payments p
        JOIN supplier_payment_allocations x ON x.supplier_payment_id = p.supplier_payment_id AND x.allocation_status = 'ACTIVE'
        GROUP BY p.supplier_payment_id, p.amount HAVING SUM(x.allocated_amount) > p.amount""")]
    return found
