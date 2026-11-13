"""GST rules (1E §7, V-TX-*). Each check returns the violating rows.

Step 3.7c implements V-TX-01 and V-TX-02 for supplier invoices (INPUT), step 3.9a adds sales invoices
(OUTPUT), 3.9b credit notes, 3.12a inter-GSTIN transfers (two rows per line); expenses add theirs with 3.13. V-TX-03 (returns) comes with GST filing (3.14).
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
               i.supplier_id, NULL AS customer_id, i.supplier_gstin AS counterparty_gstin, NULL AS gstr1_section
        FROM supplier_invoice_line_items sl JOIN supplier_invoices i ON i.supplier_invoice_id = sl.supplier_invoice_id
        JOIN suppliers s ON s.supplier_id = i.supplier_id
        WHERE i.invoice_status = 'APPROVED' AND s.gst_registration_type = 'REGISTERED'"""),
    # Sales invoices (3.9a): every line of an issued (or later cancelled) invoice, with its GSTR-1 section:
    # B2B for a registered buyer, B2CL for an unregistered inter-state invoice above the threshold, else B2CS.
    "SALES_INVOICE": ("invoice_line_item_id", """
        SELECT il.invoice_line_item_id AS source_id, il.taxable_amount, il.cgst_amount, il.sgst_amount,
               il.igst_amount, il.gst_rate, i.company_gst_registration_id, i.invoice_date AS document_date,
               NULL AS supplier_id, i.customer_id, i.customer_gstin AS counterparty_gstin,
               CASE WHEN i.customer_gstin IS NOT NULL THEN 'B2B'
                    WHEN i.is_inter_state AND i.total_amount > {b2cl} THEN 'B2CL' ELSE 'B2CS' END AS gstr1_section
        FROM invoice_line_items il JOIN invoices i ON i.invoice_id = il.invoice_id"""),
    # Credit notes (3.9b): negative OUTPUT rows, CDNR for a registered buyer, CDNUR otherwise.
    "CREDIT_NOTE": ("credit_note_line_item_id", """
        SELECT cl.credit_note_line_item_id AS source_id, -cl.taxable_amount AS taxable_amount,
               -cl.cgst_amount AS cgst_amount, -cl.sgst_amount AS sgst_amount, -cl.igst_amount AS igst_amount,
               cl.gst_rate, n.company_gst_registration_id, n.credit_note_date AS document_date, NULL AS supplier_id,
               n.customer_id, i.customer_gstin AS counterparty_gstin,
               IF(i.customer_gstin IS NULL, 'CDNUR', 'CDNR') AS gstr1_section
        FROM credit_note_line_items cl JOIN credit_notes n ON n.credit_note_id = cl.credit_note_id
        JOIN invoices i ON i.invoice_id = n.invoice_id WHERE n.credit_note_status = 'ISSUED'"""),
    # Inter-GSTIN transfers (3.12a, D29): an OUTPUT row at the source GSTIN (B2B, the destination GSTIN as the
    # buyer) and an INPUT row at the destination, IGST at the line's rate; same-GSTIN transfers have none.
    "TRANSFER_OUT": ("stock_transfer_line_item_id", """
        SELECT l.stock_transfer_line_item_id AS source_id, l.transfer_value AS taxable_amount, 0 AS cgst_amount,
               0 AS sgst_amount, l.igst_amount, l.gst_rate, s.company_gst_registration_id,
               DATE(t.dispatched_at) AS document_date, NULL AS supplier_id, NULL AS customer_id,
               g.gstin AS counterparty_gstin, 'B2B' AS gstr1_section
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        JOIN warehouses s ON s.warehouse_id = t.source_warehouse_id
        JOIN warehouses d ON d.warehouse_id = t.destination_warehouse_id
        JOIN company_gst_registrations g ON g.company_gst_registration_id = d.company_gst_registration_id
        WHERE t.is_inter_gstin AND t.dispatched_at IS NOT NULL"""),
    "TRANSFER_IN": ("stock_transfer_line_item_id", """
        SELECT l.stock_transfer_line_item_id AS source_id, l.transfer_value AS taxable_amount, 0 AS cgst_amount,
               0 AS sgst_amount, l.igst_amount, l.gst_rate, d.company_gst_registration_id,
               DATE(t.dispatched_at) AS document_date, NULL AS supplier_id, NULL AS customer_id,
               g.gstin AS counterparty_gstin, NULL AS gstr1_section
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        JOIN warehouses s ON s.warehouse_id = t.source_warehouse_id
        JOIN warehouses d ON d.warehouse_id = t.destination_warehouse_id
        JOIN company_gst_registrations g ON g.company_gst_registration_id = s.company_gst_registration_id
        WHERE t.is_inter_gstin AND t.dispatched_at IS NOT NULL"""),
    # Expenses (3.13): every GST line of an approved bill of a registered supplier, eligible or not (ITC
    # eligibility is on the row; only eligible credit reaches the Input GST accounts, V-TX-02).
    "EXPENSE": ("expense_line_item_id", """
        SELECT l.expense_line_item_id AS source_id, l.taxable_amount, l.cgst_amount, l.sgst_amount, l.igst_amount,
               l.gst_rate, e.company_gst_registration_id, e.expense_date AS document_date, e.supplier_id,
               NULL AS customer_id, s.gstin AS counterparty_gstin, NULL AS gstr1_section
        FROM expense_line_items l JOIN expenses e ON e.expense_id = l.expense_id
        JOIN suppliers s ON s.supplier_id = e.supplier_id
        WHERE e.expense_status = 'APPROVED' AND s.gst_registration_type = 'REGISTERED' AND l.gst_rate > 0"""),
}
DEFAULT_B2CL_THRESHOLD = 100000


def b2cl_threshold(ctx: ValidationContext) -> int:
    """GSTR-1 B2CL invoice value the generator used (``fulfilment.b2cl_threshold`` in the latest run's config)."""
    snapshot = ctx.scalar("SELECT JSON_EXTRACT(config_snapshot, '$.fulfilment.b2cl_threshold') FROM generation_runs "
                          "WHERE config_snapshot IS NOT NULL ORDER BY generation_run_id DESC LIMIT 1")
    return int(snapshot) if snapshot not in (None, "null") else DEFAULT_B2CL_THRESHOLD


@check("V-TX-01")
def tax_rows_equal_their_source_lines(ctx: ValidationContext) -> Rows:
    """One tax transaction per taxable source line (approved bills of registered suppliers …) with the line's
    taxable value, rate, tax amounts, GSTIN and date; none for other lines; no document types not generated yet."""
    found: Rows = []
    for kind, (column, lines) in TAX_SOURCES.items():
        lines = lines.replace("{b2cl}", str(b2cl_threshold(ctx)))
        found += [{**r, "document_type": kind} for r in ctx.rows(f"""
            SELECT COALESCE(s.source_id, t.{column}) AS source_id,
                   CASE WHEN s.source_id IS NULL THEN 'tax row without a taxable source line'
                        WHEN t.tax_transaction_id IS NULL THEN 'taxable line without a tax row'
                        ELSE 'amounts, rate, GSTIN or date differ' END AS problem
            FROM ({lines}) s
            LEFT JOIN tax_transactions t ON t.{column} = s.source_id AND NOT t.is_cancellation
                 AND t.document_type = '{kind}'
            WHERE t.tax_transaction_id IS NULL OR t.taxable_value <> s.taxable_amount OR t.cgst_amount <> s.cgst_amount
               OR t.sgst_amount <> s.sgst_amount OR t.igst_amount <> s.igst_amount OR t.gst_rate <> s.gst_rate
               OR t.company_gst_registration_id <> s.company_gst_registration_id OR t.document_date <> s.document_date
               OR NOT (t.supplier_id <=> s.supplier_id) OR NOT (t.customer_id <=> s.customer_id)
               OR NOT (t.counterparty_gstin <=> s.counterparty_gstin) OR NOT (t.gstr1_section <=> s.gstr1_section)
            UNION ALL
            SELECT t.{column}, 'tax row without a taxable source line' FROM tax_transactions t
            WHERE t.document_type = '{kind}' AND t.{column} NOT IN (SELECT source_id FROM ({lines}) s2)""")]
    found += [{**r, "problem": "cancelled invoice line without exactly one mirror row (or a mirror of a live one)"}
              for r in ctx.rows("""
        SELECT il.invoice_line_item_id, i.invoice_status, COUNT(t.tax_transaction_id) AS mirrors FROM invoice_line_items il
        JOIN invoices i ON i.invoice_id = il.invoice_id
        LEFT JOIN tax_transactions t ON t.invoice_line_item_id = il.invoice_line_item_id AND t.is_cancellation
             AND t.taxable_value = -il.taxable_amount AND t.cgst_amount = -il.cgst_amount
             AND t.sgst_amount = -il.sgst_amount AND t.igst_amount = -il.igst_amount
        GROUP BY il.invoice_line_item_id, i.invoice_status
        HAVING COUNT(t.tax_transaction_id) <> (i.invoice_status = 'CANCELLED')
        UNION ALL
        SELECT t.invoice_line_item_id, 'mirror differs', 1 FROM tax_transactions t
        JOIN invoice_line_items il ON il.invoice_line_item_id = t.invoice_line_item_id
        WHERE t.is_cancellation AND (t.taxable_value <> -il.taxable_amount OR t.igst_amount <> -il.igst_amount
                                     OR t.cgst_amount <> -il.cgst_amount)""")]
    found += [{"document_type": t, "problem": "document type not generated yet"} for (t,) in ctx.conn.exec_driver_sql(
        "SELECT DISTINCT document_type FROM tax_transactions") if t not in TAX_SOURCES]
    return found


@check("V-TX-02")
def tax_rows_equal_gl_tax_accounts(ctx: ValidationContext) -> Rows:
    """Σ tax transactions per GSTIN × return period × component × direction = the movement on that GSTIN's tax
    account (input credit that isn't eligible is expensed, 3.13, so its rows aren't on the tax accounts). Journals are attributed by their document date (the tax document's date, also when D59 moved the
    posting into a later open period); GST settlement journals (P22) move balances between heads and are left out."""
    return ctx.rows("""
        SELECT k.company_gst_registration_id, k.period, k.tax_component, k.tax_direction,
               COALESCE(t.amount, 0) AS tax_rows, COALESCE(g.amount, 0) AS gl
        FROM (SELECT ta.company_gst_registration_id, ta.tax_component, ta.tax_direction, ta.gl_account_id, p.period
              FROM tax_accounts ta
              CROSS JOIN (SELECT DISTINCT return_period AS period FROM tax_transactions
                          UNION SELECT DISTINCT DATE_FORMAT(document_date, '%Y-%m') FROM journal_entries) p) k
        LEFT JOIN (SELECT company_gst_registration_id, return_period AS period, tax_direction, 'CGST' AS tax_component,
                          SUM(cgst_amount) AS amount FROM tax_transactions WHERE itc_eligibility <> 'INELIGIBLE' GROUP BY 1, 2, 3
                   UNION ALL
                   SELECT company_gst_registration_id, return_period, tax_direction, 'SGST', SUM(sgst_amount)
                   FROM tax_transactions WHERE itc_eligibility <> 'INELIGIBLE' GROUP BY 1, 2, 3
                   UNION ALL
                   SELECT company_gst_registration_id, return_period, tax_direction, 'IGST', SUM(igst_amount)
                   FROM tax_transactions WHERE itc_eligibility <> 'INELIGIBLE' GROUP BY 1, 2, 3) t
               ON t.company_gst_registration_id = k.company_gst_registration_id AND t.period = k.period
              AND t.tax_component = k.tax_component AND t.tax_direction = k.tax_direction
        LEFT JOIN (SELECT l.account_id, DATE_FORMAT(j.document_date, '%Y-%m') AS period,
                          SUM(l.debit_amount - l.credit_amount) AS amount
                   FROM journal_entry_lines l JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                   WHERE j.journal_source <> 'GST_SETTLEMENT' GROUP BY 1, 2) g
               ON g.account_id = k.gl_account_id AND g.period = k.period
        WHERE COALESCE(t.amount, 0) <> COALESCE(IF(k.tax_direction = 'INPUT', g.amount, -g.amount), 0)""")
