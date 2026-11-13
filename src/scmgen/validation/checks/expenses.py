"""Expense rules (1E §7 V-EX-*, 1D V-SH-05; step 3.13). Each check returns the violating rows."""

from __future__ import annotations

import json
from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


def _cost_centre_rules(ctx: ValidationContext) -> dict[str, str]:
    """``expenses.cost_centres`` from the latest run's configuration snapshot."""
    snapshot = ctx.scalar("SELECT JSON_EXTRACT(config_snapshot, '$.expenses.cost_centres') FROM generation_runs "
                          "WHERE config_snapshot IS NOT NULL ORDER BY generation_run_id DESC LIMIT 1")
    return json.loads(snapshot) if snapshot not in (None, "null") else {}


@check("V-EX-01")
def expense_totals(ctx: ValidationContext) -> Rows:
    """Header amounts = Σ lines (+ round-off); the ITC-eligible amount = Σ GST of eligible lines; every APPROVED
    expense has its P19 journal for its total."""
    found = [{**r, "problem": "header amounts differ from the lines"} for r in ctx.rows("""
        SELECT e.expense_id, e.taxable_amount, s.taxable, e.total_amount, s.line_total, e.round_off_amount,
               e.itc_eligible_tax_amount, s.eligible
        FROM expenses e JOIN (SELECT expense_id, SUM(taxable_amount) AS taxable, SUM(cgst_amount) AS cgst,
                                     SUM(sgst_amount) AS sgst, SUM(igst_amount) AS igst, SUM(line_total) AS line_total,
                                     SUM(IF(is_itc_eligible, cgst_amount + sgst_amount + igst_amount, 0)) AS eligible
                              FROM expense_line_items GROUP BY expense_id) s ON s.expense_id = e.expense_id
        WHERE e.taxable_amount <> s.taxable OR e.cgst_amount <> s.cgst OR e.sgst_amount <> s.sgst
           OR e.igst_amount <> s.igst OR e.total_amount <> s.line_total + e.round_off_amount
           OR e.itc_eligible_tax_amount <> s.eligible""")]
    found += [{**r, "problem": "expense without lines"} for r in ctx.rows("""
        SELECT e.expense_id FROM expenses e
        WHERE NOT EXISTS (SELECT 1 FROM expense_line_items l WHERE l.expense_id = e.expense_id)""")]
    found += [{**r, "problem": "approved expense without its P19 journal for its total (or a journal for another)"}
              for r in ctx.rows("""
        SELECT e.expense_id, e.expense_status, e.total_amount, j.total_debit FROM expenses e
        LEFT JOIN journal_entries j ON j.expense_id = e.expense_id AND j.journal_source = 'EXPENSE'
        WHERE (e.expense_status = 'APPROVED') <> (j.journal_entry_id IS NOT NULL)
           OR (j.journal_entry_id IS NOT NULL AND j.total_debit < e.total_amount - e.round_off_amount - 1)""")]
    return found


@check("V-EX-02")
def expense_categories_and_cost_centres(ctx: ValidationContext) -> Rows:
    """A campaign only on MARKETING categories, with the expense date within its window ± 15 days; PAYROLL ⇔ the
    salaries category; each line's cost centre follows its category's rule (a warehouse's, a business unit's,
    the claimant's, or the named department's)."""
    found = [{**r, "problem": "campaign on a non-marketing line or outside its window ± 15 days"} for r in ctx.rows("""
        SELECT l.expense_line_item_id, c.category_code, e.expense_date, m.start_date, m.end_date
        FROM expense_line_items l JOIN expenses e ON e.expense_id = l.expense_id
        JOIN expense_categories c ON c.expense_category_id = l.expense_category_id
        JOIN marketing_campaigns m ON m.marketing_campaign_id = l.marketing_campaign_id
        WHERE c.category_group <> 'MARKETING' OR e.expense_date < m.start_date - INTERVAL 15 DAY
           OR e.expense_date > m.end_date + INTERVAL 15 DAY""")]
    found += [{**r, "problem": "payroll line outside the salaries category, or salaries outside payroll"}
              for r in ctx.rows("""
        SELECT l.expense_line_item_id, e.expense_type, c.category_code FROM expense_line_items l
        JOIN expenses e ON e.expense_id = l.expense_id
        JOIN expense_categories c ON c.expense_category_id = l.expense_category_id
        WHERE (e.expense_type = 'PAYROLL') <> (c.category_code = 'SALARIES')""")]
    for category, rule in sorted(_cost_centre_rules(ctx).items()):
        if rule == "WAREHOUSE":
            condition = "cc.warehouse_id IS NULL OR (e.warehouse_id IS NOT NULL AND cc.warehouse_id <> e.warehouse_id)"
        elif rule == "BUSINESS_UNIT":
            condition = "cc.business_unit_id IS NULL"
        elif rule == "EMPLOYEE":
            condition = ("(e.employee_id IS NOT NULL AND cc.cost_center_id <> emp.cost_center_id) OR "
                         "(e.employee_id IS NULL AND NOT EXISTS (SELECT 1 FROM employees x "
                         "WHERE x.cost_center_id = cc.cost_center_id))")
        else:
            condition = f"d.department_code IS NULL OR d.department_code <> '{rule}'"
        found += [{**r, "problem": f"cost centre doesn't follow the {rule} rule"} for r in ctx.rows(f"""
            SELECT l.expense_line_item_id, c.category_code, cc.cost_center_code FROM expense_line_items l
            JOIN expenses e ON e.expense_id = l.expense_id
            JOIN expense_categories c ON c.expense_category_id = l.expense_category_id
            JOIN cost_centers cc ON cc.cost_center_id = l.cost_center_id
            LEFT JOIN departments d ON d.department_id = cc.department_id
            LEFT JOIN employees emp ON emp.employee_id = e.employee_id
            WHERE c.category_code = '{category}' AND ({condition})""")]
    return found


@check("V-EX-03")
def expense_payments(ctx: ValidationContext) -> Rows:
    """Payments only for APPROVED expenses, Σ COMPLETED ≤ total, each COMPLETED one with its P20 journal; petty
    cash only from its warehouse's petty-cash account, in cash, ≤ ₹10,000, and nothing else from petty cash;
    payroll from the PAYROLL account (PAYMENTS when there is none); payroll = Σ salaries of the employees active in
    the month, pro-rated by days."""
    found = [{**r, "problem": "payments exceed the total or pay an unapproved expense"} for r in ctx.rows("""
        SELECT e.expense_id, e.expense_status, e.total_amount, SUM(p.amount) AS paid FROM expenses e
        JOIN expense_payments p ON p.expense_id = e.expense_id AND p.payment_status = 'COMPLETED'
        GROUP BY e.expense_id, e.expense_status, e.total_amount
        HAVING e.expense_status <> 'APPROVED' OR SUM(p.amount) > e.total_amount""")]
    found += [{**r, "problem": "completed payment without its P20 journal of the amount"} for r in ctx.rows("""
        SELECT p.expense_payment_id, p.amount, j.total_debit FROM expense_payments p
        LEFT JOIN journal_entries j ON j.expense_payment_id = p.expense_payment_id
        WHERE (p.payment_status = 'COMPLETED') <> (j.journal_entry_id IS NOT NULL) OR j.total_debit <> p.amount""")]
    found += [{**r, "problem": "petty cash paid otherwise, or petty cash used for another expense"} for r in ctx.rows("""
        SELECT p.expense_payment_id, e.expense_type, b.account_purpose, m.method_code, p.amount
        FROM expense_payments p JOIN expenses e ON e.expense_id = p.expense_id
        JOIN bank_accounts b ON b.bank_account_id = p.bank_account_id
        JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
        WHERE (e.expense_type = 'PETTY_CASH') <> (b.account_purpose = 'PETTY_CASH')
           OR (e.expense_type = 'PETTY_CASH' AND (b.warehouse_id <> e.warehouse_id OR m.method_code <> 'CASH'
                                                   OR p.amount > 10000))""")]
    found += [{**r, "problem": "payroll paid from another account"} for r in ctx.rows("""
        SELECT p.expense_payment_id, b.account_purpose FROM expense_payments p
        JOIN expenses e ON e.expense_id = p.expense_id JOIN bank_accounts b ON b.bank_account_id = p.bank_account_id
        WHERE e.expense_type = 'PAYROLL'
          AND b.account_purpose <> IF(EXISTS (SELECT 1 FROM bank_accounts x WHERE x.account_purpose = 'PAYROLL'
                                                AND x.account_status = 'ACTIVE'), 'PAYROLL', 'PAYMENTS')""")]
    found += [{**r, "problem": "payroll line ≠ Σ pro-rated salaries of its cost centre's active employees"}
              for r in ctx.rows("""
        SELECT e.expense_id, l.cost_center_id, l.taxable_amount, COALESCE(s.salaries, 0) AS salaries
        FROM expenses e JOIN expense_line_items l ON l.expense_id = e.expense_id
        LEFT JOIN (SELECT p.expense_id, x.cost_center_id,
                          SUM(ROUND(x.monthly_gross_salary
                                    * (DATEDIFF(LEAST(p.service_period_end, COALESCE(x.termination_date, p.service_period_end)),
                                                GREATEST(p.service_period_start, x.hire_date)) + 1)
                                    / DAY(p.service_period_end), 2)) AS salaries
                   FROM expenses p JOIN employees x ON x.hire_date <= p.service_period_end
                        AND (x.termination_date IS NULL OR x.termination_date >= p.service_period_start)
                   WHERE p.expense_type = 'PAYROLL' GROUP BY p.expense_id, x.cost_center_id) s
               ON s.expense_id = e.expense_id AND s.cost_center_id = l.cost_center_id
        WHERE e.expense_type = 'PAYROLL' AND l.taxable_amount <> COALESCE(s.salaries, 0)""")]
    return found


@check("V-SH-05")
def freight_bills_match_shipments(ctx: ValidationContext) -> Rows:
    """★ A carrier's monthly freight bill (Σ FREIGHT_OUTWARD lines, recurrence key ``FREIGHT-<carrier>``) = Σ its
    shipments' freight cost dispatched in that month, per warehouse line."""
    return ctx.rows("""
        SELECT e.expense_id, e.recurrence_key, e.service_period_start, l.cost_center_id, l.taxable_amount,
               COALESCE(s.freight, 0) AS shipments
        FROM expenses e JOIN expense_line_items l ON l.expense_id = e.expense_id
        JOIN cost_centers cc ON cc.cost_center_id = l.cost_center_id
        LEFT JOIN (SELECT shipping_carrier_id, warehouse_id, DATE_FORMAT(dispatched_at, '%Y-%m-01') AS month,
                          SUM(freight_cost_amount) AS freight
                   FROM shipments WHERE dispatched_at IS NOT NULL GROUP BY 1, 2, 3) s
               ON CONCAT('FREIGHT-', s.shipping_carrier_id) = e.recurrence_key AND s.warehouse_id = cc.warehouse_id
              AND s.month = DATE_FORMAT(e.service_period_start, '%Y-%m-01')
        WHERE e.recurrence_key LIKE 'FREIGHT-%' AND e.expense_status <> 'REJECTED'
          AND l.taxable_amount <> COALESCE(s.freight, 0)""")
