"""Accounting rules (1B §7 V-AC-01…07, 1E §7 V-AC-10…14). Each check returns the violating rows.

Not implemented yet: V-AC-12/13 (they need the documents and period balances of later steps).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from scmgen.domain.journal import SYSTEM_ROLES
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-AC-01", requires_master_data=True)
def account_code_prefix(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT a.account_id, a.account_code, t.account_type_code, t.code_prefix
        FROM chart_of_accounts a JOIN account_types t ON t.account_type_id = a.account_type_id
        WHERE LEFT(a.account_code, 1) <> t.code_prefix""")


@check("V-AC-02", requires_master_data=True)
def leaves_postable_headers_have_children(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT a.account_id, a.account_code, a.is_postable,
               CASE WHEN a.is_postable THEN 'postable account has children' ELSE 'header account has no children' END AS problem
        FROM chart_of_accounts a
        WHERE a.is_postable = EXISTS (SELECT 1 FROM chart_of_accounts c WHERE c.parent_account_id = a.account_id)""")


@check("V-AC-03", requires_master_data=True)
def mapped_accounts_have_the_right_type(ctx: ValidationContext) -> Rows:
    """BU revenue / returns / COGS, expense categories, bank GL and tax accounts point to postable accounts of
    the expected type."""
    return ctx.rows("""
        SELECT m.source, m.source_id, a.account_code, t.account_type_code, a.is_postable
        FROM (
            SELECT 'bu revenue' AS source, business_unit_id AS source_id, revenue_account_id AS account_id, 'REVENUE' AS expected FROM business_units
            UNION ALL SELECT 'bu returns', business_unit_id, sales_returns_account_id, 'REVENUE' FROM business_units
            UNION ALL SELECT 'bu cogs', business_unit_id, cogs_account_id, 'COGS' FROM business_units
            UNION ALL SELECT 'expense category', expense_category_id, gl_account_id, 'EXPENSE|COGS' FROM expense_categories
            UNION ALL SELECT 'bank account', bank_account_id, gl_account_id,
                             IF(account_type = 'CASH_CREDIT', 'LIABILITY', 'ASSET') FROM bank_accounts
            UNION ALL SELECT 'tax account', tax_account_id, gl_account_id,
                             IF(tax_direction = 'INPUT', 'ASSET', 'LIABILITY') FROM tax_accounts
        ) m
        JOIN chart_of_accounts a ON a.account_id = m.account_id
        JOIN account_types t ON t.account_type_id = a.account_type_id
        WHERE NOT a.is_postable OR NOT FIND_IN_SET(t.account_type_code, REPLACE(m.expected, '|', ','))""")


@check("V-AC-04", requires_master_data=True)
def system_roles_exist(ctx: ValidationContext) -> Rows:
    present = {r["system_role"] for r in ctx.rows("SELECT system_role FROM chart_of_accounts WHERE system_role IS NOT NULL")}
    return [{"system_role": role, "problem": "no account carries this role"} for role in sorted(SYSTEM_ROLES - present)]


@check("V-AC-05", requires_master_data=True)
def periods_contiguous_and_mostly_closed(ctx: ValidationContext) -> Rows:
    """Contiguous, 12 per FY starting in fy_start_month; at most the current and previous months OPEN."""
    found: Rows = []
    periods = ctx.rows("SELECT accounting_period_id, fiscal_year, period_number, start_date, end_date, period_status "
                       "FROM accounting_periods ORDER BY start_date")
    start_month = ctx.scalar("SELECT fy_start_month FROM companies")
    for earlier, later in zip(periods, periods[1:]):
        if (later["start_date"] - earlier["end_date"]).days != 1:
            found.append({"period": later["accounting_period_id"], "problem": "gap or overlap with the previous period"})
    for row in ctx.rows("SELECT fiscal_year, COUNT(*) AS n FROM accounting_periods GROUP BY fiscal_year HAVING n <> 12"):
        found.append({**row, "problem": "fiscal year without exactly 12 periods"})
    for p in periods:
        if p["period_number"] == 1 and p["start_date"].month != start_month:
            found.append({"period": p["accounting_period_id"], "problem": "period 1 does not start in fy_start_month"})
    committed = ctx.scalar("SELECT MAX(committed_through) FROM generation_runs")
    if committed is not None:
        month_start = committed.date().replace(day=1)
        previous_start = (month_start - timedelta(days=1)).replace(day=1)
        for p in periods:
            if p["period_status"] == "OPEN" and p["start_date"] < previous_start:
                found.append({"period": p["accounting_period_id"], "problem": "older than the previous month but still OPEN"})
    return found


@check("V-AC-06", requires_master_data=True)
def tax_accounts_complete(ctx: ValidationContext) -> Rows:
    found = ctx.rows("""
        SELECT r.company_gst_registration_id, COUNT(t.tax_account_id) AS accounts
        FROM company_gst_registrations r LEFT JOIN tax_accounts t ON t.company_gst_registration_id = r.company_gst_registration_id
        GROUP BY r.company_gst_registration_id HAVING accounts <> 6""")
    found += ctx.rows("""
        SELECT t.tax_account_id, t.tax_component, s.intra_state_tax_component, 'SGST/UTGST does not match the state' AS problem
        FROM tax_accounts t JOIN company_gst_registrations r ON r.company_gst_registration_id = t.company_gst_registration_id
        JOIN states s ON s.state_id = r.state_id
        WHERE t.tax_component IN ('SGST', 'UTGST') AND t.tax_component <> s.intra_state_tax_component""")
    return found


@check("V-AC-07", requires_master_data=True)
def one_cost_centre_each(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT 'business unit' AS kind, b.business_unit_id AS id FROM business_units b
        WHERE NOT EXISTS (SELECT 1 FROM cost_centers c WHERE c.business_unit_id = b.business_unit_id)
        UNION ALL SELECT 'warehouse', w.warehouse_id FROM warehouses w
        WHERE NOT EXISTS (SELECT 1 FROM cost_centers c WHERE c.warehouse_id = w.warehouse_id)
        UNION ALL SELECT 'department', d.department_id FROM departments d
        WHERE NOT EXISTS (SELECT 1 FROM cost_centers c WHERE c.department_id = d.department_id)""")


@check("V-AC-10")
def journals_balance_to_their_lines(ctx: ValidationContext) -> Rows:
    """Σ debit = Σ credit = header totals (the posting key's uniqueness is enforced by the database)."""
    return ctx.rows("""
        SELECT j.journal_entry_id, j.total_debit, j.total_credit,
               COALESCE(SUM(l.debit_amount), 0) AS line_debit, COALESCE(SUM(l.credit_amount), 0) AS line_credit
        FROM journal_entries j LEFT JOIN journal_entry_lines l ON l.journal_entry_id = j.journal_entry_id
        GROUP BY j.journal_entry_id, j.total_debit, j.total_credit
        HAVING line_debit <> line_credit OR line_debit <> j.total_debit OR line_credit <> j.total_credit""")


@check("V-AC-11")
def journal_line_rules(ctx: ValidationContext) -> Rows:
    """Postable account; cost centre iff required; party iff AR/AP control; warehouse iff Inventory control."""
    return ctx.rows("""
        SELECT l.journal_entry_line_id, a.account_code,
               CASE WHEN NOT a.is_postable THEN 'header account'
                    WHEN a.requires_cost_center <> (l.cost_center_id IS NOT NULL) THEN 'cost centre required iff P&L'
                    WHEN (a.control_subledger = 'AR') <> (l.customer_id IS NOT NULL) THEN 'customer iff AR control'
                    WHEN (a.control_subledger = 'AP') <> (l.supplier_id IS NOT NULL OR l.employee_id IS NOT NULL)
                         THEN 'supplier/employee iff AP control'
                    ELSE 'warehouse iff Inventory control' END AS problem
        FROM journal_entry_lines l JOIN chart_of_accounts a ON a.account_id = l.account_id
        WHERE NOT a.is_postable
           OR a.requires_cost_center <> (l.cost_center_id IS NOT NULL)
           OR (COALESCE(a.control_subledger, '') = 'AR') <> (l.customer_id IS NOT NULL)
           OR (COALESCE(a.control_subledger, '') = 'AP') <> (l.supplier_id IS NOT NULL OR l.employee_id IS NOT NULL)
           OR (COALESCE(a.control_subledger, '') = 'INVENTORY') <> (l.warehouse_id IS NOT NULL)""")


@check("V-AC-14")
def postings_into_open_periods(ctx: ValidationContext) -> Rows:
    """Posting date inside its period, which was OPEN when the journal was recorded; a later posting date
    than the document date only when the document's own period was already closed (D59)."""
    return ctx.rows("""
        SELECT j.journal_entry_id, j.document_date, j.posting_date, p.period_code,
               CASE WHEN j.posting_date NOT BETWEEN p.start_date AND p.end_date THEN 'posting date outside its period'
                    WHEN p.opened_at IS NULL OR j.created_at < p.opened_at
                         OR (p.closed_at IS NOT NULL AND j.created_at > p.closed_at) THEN 'period not OPEN at posting'
                    ELSE 'posted later than its document although its period was open (D59)' END AS problem
        FROM journal_entries j JOIN accounting_periods p ON p.accounting_period_id = j.accounting_period_id
        LEFT JOIN accounting_periods own ON j.document_date BETWEEN own.start_date AND own.end_date
        WHERE j.posting_date NOT BETWEEN p.start_date AND p.end_date
           OR p.opened_at IS NULL OR j.created_at < p.opened_at
           OR (p.closed_at IS NOT NULL AND j.created_at > p.closed_at)
           OR (j.posting_date > j.document_date AND j.journal_type <> 'REVERSAL'
               AND own.opened_at IS NOT NULL AND own.opened_at <= j.created_at
               AND (own.closed_at IS NULL OR own.closed_at >= j.created_at))""")
