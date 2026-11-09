-- Workspace schema, batch 6 (Part 1E finance), file 5/5: the general ledger as a derived view (R6).
-- Design: docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md §3. Journal lines stay the only
-- accounting source of truth; the view never competes with them. Both POSTED and REVERSED entries are
-- included (an entry and its reversal net to zero). Running balance is debit-positive.

CREATE VIEW v_general_ledger AS
SELECT
    jel.journal_entry_line_id,
    je.journal_entry_id,
    je.journal_number,
    je.posting_date,
    je.document_date,
    ap.fiscal_year,
    ap.period_code,
    je.journal_source,
    je.entry_status,
    je.description,
    coa.account_id,
    coa.account_code,
    coa.account_name,
    at.account_type_code,
    jel.debit_amount,
    jel.credit_amount,
    jel.debit_amount - jel.credit_amount AS net_amount,
    SUM(jel.debit_amount - jel.credit_amount) OVER (
        PARTITION BY jel.account_id
        ORDER BY je.posting_date, je.journal_entry_id, jel.line_number
        ROWS UNBOUNDED PRECEDING) AS running_balance,
    jel.cost_center_id,
    jel.customer_id,
    jel.supplier_id,
    jel.employee_id,
    jel.warehouse_id
FROM journal_entry_lines jel
JOIN journal_entries je    ON je.journal_entry_id = jel.journal_entry_id
JOIN accounting_periods ap ON ap.accounting_period_id = je.accounting_period_id
JOIN chart_of_accounts coa ON coa.account_id = jel.account_id
JOIN account_types at      ON at.account_type_id = coa.account_type_id;
