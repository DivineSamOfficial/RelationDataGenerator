"""GST rules (1E §7, V-TX-*). Each check returns the violating rows.

Step 3.7c implements V-TX-01 and V-TX-02 for supplier invoices (INPUT); sales, credit notes, expenses and
transfers add their source lines as their steps arrive. V-TX-03 (returns) comes with GST filing (3.14).
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# V-TX-01: document type → (source column, the lines that must have exactly one row, with their amounts).
TAX_SOURCES = {
    "SUPPLIER_INVOICE": ("supplier_invoice_line_item_id", """
        SELECT sl.supplier_invoice_line_item_id AS source_id, sl.taxable_amount, sl.cgst_amount, sl.sgst_amount,
               sl.igst_amount, sl.gst_rate, i.company_gst_registration_id, i.invoice_date AS document_date,
               i.supplier_id
        FROM supplier_invoice_line_items sl JOIN supplier_invoices i ON i.supplier_invoice_id = sl.supplier_invoice_id
        JOIN suppliers s ON s.supplier_id = i.supplier_id
        WHERE i.invoice_status = 'APPROVED' AND s.gst_registration_type = 'REGISTERED'"""),
}


@check("V-TX-01")
def tax_rows_equal_their_source_lines(ctx: ValidationContext) -> Rows:
    """One tax transaction per taxable source line (approved bills of registered suppliers …) with the line's
    taxable value, rate, tax amounts, GSTIN and date; none for other lines; no document types not generated yet."""
    found: Rows = []
    for kind, (column, lines) in TAX_SOURCES.items():
        found += [{**r, "document_type": kind} for r in ctx.rows(f"""
            SELECT COALESCE(s.source_id, t.{column}) AS source_id,
                   CASE WHEN s.source_id IS NULL THEN 'tax row without a taxable source line'
                        WHEN t.tax_transaction_id IS NULL THEN 'taxable line without a tax row'
                        ELSE 'amounts, rate, GSTIN or date differ' END AS problem
            FROM ({lines}) s
            LEFT JOIN tax_transactions t ON t.{column} = s.source_id
            WHERE t.tax_transaction_id IS NULL OR t.taxable_value <> s.taxable_amount OR t.cgst_amount <> s.cgst_amount
               OR t.sgst_amount <> s.sgst_amount OR t.igst_amount <> s.igst_amount OR t.gst_rate <> s.gst_rate
               OR t.company_gst_registration_id <> s.company_gst_registration_id OR t.document_date <> s.document_date
               OR NOT (t.supplier_id <=> s.supplier_id)
            UNION ALL
            SELECT t.{column}, 'tax row without a taxable source line' FROM tax_transactions t
            WHERE t.document_type = '{kind}' AND t.{column} NOT IN (SELECT source_id FROM ({lines}) s2)""")]
    found += [{"document_type": t, "problem": "document type not generated yet"} for (t,) in ctx.conn.exec_driver_sql(
        "SELECT DISTINCT document_type FROM tax_transactions") if t not in TAX_SOURCES]
    return found


@check("V-TX-02")
def tax_rows_equal_gl_tax_accounts(ctx: ValidationContext) -> Rows:
    """Σ tax transactions per GSTIN × return period × component × direction = the movement on that GSTIN's tax
    account. Journals are attributed by their document date (the tax document's date, also when D59 moved the
    posting into a later open period); GST settlement journals (P22) move balances between heads and are left out."""
    return ctx.rows("""
        SELECT k.company_gst_registration_id, k.period, k.tax_component, k.tax_direction,
               COALESCE(t.amount, 0) AS tax_rows, COALESCE(g.amount, 0) AS gl
        FROM (SELECT ta.company_gst_registration_id, ta.tax_component, ta.tax_direction, ta.gl_account_id, p.period
              FROM tax_accounts ta
              CROSS JOIN (SELECT DISTINCT return_period AS period FROM tax_transactions
                          UNION SELECT DISTINCT DATE_FORMAT(document_date, '%Y-%m') FROM journal_entries) p) k
        LEFT JOIN (SELECT company_gst_registration_id, return_period AS period, tax_direction, 'CGST' AS tax_component,
                          SUM(cgst_amount) AS amount FROM tax_transactions GROUP BY 1, 2, 3
                   UNION ALL
                   SELECT company_gst_registration_id, return_period, tax_direction, 'SGST', SUM(sgst_amount)
                   FROM tax_transactions GROUP BY 1, 2, 3
                   UNION ALL
                   SELECT company_gst_registration_id, return_period, tax_direction, 'IGST', SUM(igst_amount)
                   FROM tax_transactions GROUP BY 1, 2, 3) t
               ON t.company_gst_registration_id = k.company_gst_registration_id AND t.period = k.period
              AND t.tax_component = k.tax_component AND t.tax_direction = k.tax_direction
        LEFT JOIN (SELECT l.account_id, DATE_FORMAT(j.document_date, '%Y-%m') AS period,
                          SUM(l.debit_amount - l.credit_amount) AS amount
                   FROM journal_entry_lines l JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                   WHERE j.journal_source <> 'GST_SETTLEMENT' GROUP BY 1, 2) g
               ON g.account_id = k.gl_account_id AND g.period = k.period
        WHERE COALESCE(t.amount, 0) <> COALESCE(IF(k.tax_direction = 'INPUT', g.amount, -g.amount), 0)""")
