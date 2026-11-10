"""Procurement rules (1C §8, V-PR-*). Each check returns the violating rows.

Step 3.7a (sourcing) implements V-PR-01…07, 3.7b (receiving) V-PR-08…11, 3.7c (supplier invoices) V-PR-12…14.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# Allowed status transitions per document (1C §3). None = the first history row.
TRANSITIONS: dict[str, set[tuple[str | None, str]]] = {
    "rfq": {(None, "DRAFT"), ("DRAFT", "ISSUED"), ("ISSUED", "UNDER_EVALUATION"),
            ("UNDER_EVALUATION", "AWARDED"), ("UNDER_EVALUATION", "PARTIALLY_AWARDED"),
            ("UNDER_EVALUATION", "NO_AWARD"), ("DRAFT", "CANCELLED"), ("ISSUED", "CANCELLED")},
    "quotation": {(None, "RECEIVED"), ("RECEIVED", "UNDER_EVALUATION"), ("UNDER_EVALUATION", "ACCEPTED"),
                  ("UNDER_EVALUATION", "PARTIALLY_ACCEPTED"), ("UNDER_EVALUATION", "REJECTED"),
                  ("UNDER_EVALUATION", "EXPIRED"), ("RECEIVED", "WITHDRAWN")},
    "purchase_order": {(None, "DRAFT"), ("DRAFT", "PENDING_APPROVAL"), ("PENDING_APPROVAL", "APPROVED"),
                       ("APPROVED", "SENT"), ("SENT", "PARTIALLY_RECEIVED"), ("SENT", "RECEIVED"),
                       ("PARTIALLY_RECEIVED", "RECEIVED"), ("RECEIVED", "CLOSED"),
                       ("PARTIALLY_RECEIVED", "SHORT_CLOSED"), ("SHORT_CLOSED", "CLOSED"),
                       ("DRAFT", "CANCELLED"), ("PENDING_APPROVAL", "CANCELLED"), ("APPROVED", "CANCELLED"),
                       ("SENT", "CANCELLED")},
}
HEADERS = {"rfq": ("rfq_headers", "rfq_id", "rfq_status"),
           "quotation": ("quotation_headers", "quotation_id", "quotation_status"),
           "purchase_order": ("purchase_order_headers", "purchase_order_id", "po_status")}

# (header table, key, line table, quantity column) of the documents with GST amounts.
AMOUNT_DOCUMENTS = (("quotation_headers", "quotation_id", "quotation_line_items"),
                    ("purchase_order_headers", "purchase_order_id", "purchase_order_line_items"),
                    ("supplier_invoices", "supplier_invoice_id", "supplier_invoice_line_items"))


@check("V-PR-01")
def header_amounts_equal_lines(ctx: ValidationContext) -> Rows:
    """Header tax and totals = Σ lines (+ round-off to the rupee); line tax heads match the header's
    ``is_inter_state``; line GST rate = the product's rate on the document date (0 for suppliers not
    registered for GST)."""
    found: Rows = []
    for header, key, lines in AMOUNT_DOCUMENTS:
        found += [{**r, "document": header, "problem": "header ≠ Σ lines"} for r in ctx.rows(f"""
            SELECT h.{key} AS document_id, h.taxable_amount, s.taxable, h.total_amount, s.line_total
            FROM {header} h JOIN (SELECT {key}, SUM(taxable_amount) AS taxable, SUM(cgst_amount) AS cgst,
                                         SUM(sgst_amount) AS sgst, SUM(igst_amount) AS igst, SUM(line_total) AS line_total
                                  FROM {lines} GROUP BY {key}) s ON s.{key} = h.{key}
            WHERE h.taxable_amount <> s.taxable OR h.cgst_amount <> s.cgst OR h.sgst_amount <> s.sgst
               OR h.igst_amount <> s.igst OR h.total_amount <> ROUND(s.line_total, 0)
               OR h.round_off_amount <> h.total_amount - s.line_total""")]
        found += [{**r, "document": header, "problem": "header without lines"} for r in ctx.rows(
            f"SELECT h.{key} AS document_id FROM {header} h WHERE NOT EXISTS (SELECT 1 FROM {lines} l WHERE l.{key} = h.{key})")]
        found += [{**r, "document": header, "problem": "line tax heads don't match is_inter_state"} for r in ctx.rows(f"""
            SELECT h.{key} AS document_id, l.line_number FROM {header} h JOIN {lines} l ON l.{key} = h.{key}
            WHERE (h.is_inter_state AND (l.cgst_amount <> 0 OR l.sgst_amount <> 0))
               OR (NOT h.is_inter_state AND l.igst_amount <> 0)""")]
    rated = {
        "quotation_headers": """
            SELECT q.quotation_id AS document_id, ql.line_number, ql.gst_rate, rl.product_id, q.quotation_date AS on_date,
                   s.gst_registration_type
            FROM quotation_headers q JOIN quotation_line_items ql ON ql.quotation_id = q.quotation_id
            JOIN rfq_line_items rl ON rl.rfq_line_item_id = ql.rfq_line_item_id
            JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id JOIN suppliers s ON s.supplier_id = rs.supplier_id""",
        "purchase_order_headers": """
            SELECT h.purchase_order_id AS document_id, l.line_number, l.gst_rate, l.product_id, h.po_date AS on_date,
                   s.gst_registration_type
            FROM purchase_order_headers h JOIN purchase_order_line_items l ON l.purchase_order_id = h.purchase_order_id
            JOIN suppliers s ON s.supplier_id = h.supplier_id""",
    }
    for header, sql in rated.items():
        found += [{**r, "document": header, "problem": "GST rate ≠ product rate on the document date"} for r in ctx.rows(f"""
            SELECT d.document_id, d.line_number, d.gst_rate, t.gst_rate AS product_rate, d.gst_registration_type
            FROM ({sql}) d LEFT JOIN product_tax_rates t ON t.product_id = d.product_id AND t.effective_from <= d.on_date
                 AND (t.effective_to IS NULL OR t.effective_to >= d.on_date)
            WHERE d.gst_rate <> IF(d.gst_registration_type = 'REGISTERED', COALESCE(t.gst_rate, -1), 0)""")]
    return found


@check("V-PR-02")
def status_histories_are_continuous(ctx: ValidationContext) -> Rows:
    """Each document's history starts with no from-status, each from-status is the previous to-status,
    the last to-status is the header's status, and every step is an allowed transition."""
    found: Rows = []
    for document, (table, key, status_column) in HEADERS.items():
        history: dict[int, list[tuple[str | None, str]]] = defaultdict(list)
        for row in ctx.rows(f"SELECT {key} AS id, from_status, to_status FROM {document}_status_history "
                            f"ORDER BY {key}, changed_at, {document}_status_history_id"):
            history[row["id"]].append((row["from_status"], row["to_status"]))
        for row in ctx.rows(f"SELECT {key} AS id, {status_column} AS status FROM {table} ORDER BY {key}"):
            chain = history.get(row["id"], [])
            problem = None
            if not chain:
                problem = "no status history"
            elif chain[0][0] is not None:
                problem = "first entry has a from-status"
            elif any(chain[i][0] != chain[i - 1][1] for i in range(1, len(chain))):
                problem = "chain broken"
            elif chain[-1][1] != row["status"]:
                problem = f"history ends at {chain[-1][1]}, header is {row['status']}"
            else:
                bad = [step for step in chain if step not in TRANSITIONS[document]]
                if bad:
                    problem = f"transition {bad[0][0]} → {bad[0][1]} not allowed"
            if problem:
                found.append({"document": document, "document_id": row["id"], "problem": problem})
    return found


@check("V-PR-03")
def rfqs_match_business_unit_and_approvals(ctx: ValidationContext) -> Rows:
    """RFQ lines are products of the RFQ's business unit; every invited supplier was an approved supplier
    (``supplier_products``) of at least one line's product on the invitation date."""
    found = [{**r, "problem": "product outside the RFQ's business unit"} for r in ctx.rows("""
        SELECT h.rfq_id, l.line_number, p.product_id FROM rfq_headers h JOIN rfq_line_items l ON l.rfq_id = h.rfq_id
        JOIN products p ON p.product_id = l.product_id WHERE p.business_unit_id <> h.business_unit_id""")]
    found += [{**r, "problem": "invited supplier not approved for any line"} for r in ctx.rows("""
        SELECT rs.rfq_id, rs.supplier_id FROM rfq_suppliers rs
        WHERE NOT EXISTS (SELECT 1 FROM rfq_line_items l JOIN supplier_products sp ON sp.product_id = l.product_id
                          WHERE l.rfq_id = rs.rfq_id AND sp.supplier_id = rs.supplier_id
                            AND sp.approved_date <= rs.invited_date
                            AND (sp.deleted_at IS NULL OR DATE(sp.deleted_at) > rs.invited_date))""")]
    return found


@check("V-PR-04")
def quoted_invitations_have_one_quotation(ctx: ValidationContext) -> Rows:
    """QUOTED ⇔ a quotation exists (at most one is guaranteed by a UNIQUE key)."""
    return ctx.rows("""
        SELECT rs.rfq_supplier_id, rs.response_status, q.quotation_id FROM rfq_suppliers rs
        LEFT JOIN quotation_headers q ON q.rfq_supplier_id = rs.rfq_supplier_id
        WHERE (rs.response_status = 'QUOTED') <> (q.quotation_id IS NOT NULL)""")


@check("V-PR-05")
def award_statuses_agree(ctx: ValidationContext) -> Rows:
    """Quotation: ACCEPTED ⇔ every line awarded, PARTIALLY_ACCEPTED ⇔ some, other statuses ⇔ none.
    RFQ line: AWARDED ⇔ an awarded quotation line; NOT_AWARDED/OPEN ⇔ none (OPEN only while the RFQ is open).
    RFQ header: AWARDED ⇔ all lines awarded, PARTIALLY_AWARDED ⇔ some, NO_AWARD ⇔ none."""
    found = [{**r, "problem": "quotation status vs awarded lines"} for r in ctx.rows("""
        SELECT q.quotation_id, q.quotation_status, s.awarded, s.line_count FROM quotation_headers q
        JOIN (SELECT quotation_id, SUM(is_awarded) AS awarded, COUNT(*) AS line_count FROM quotation_line_items
              GROUP BY quotation_id) s ON s.quotation_id = q.quotation_id
        WHERE NOT ((q.quotation_status = 'ACCEPTED' AND s.awarded = s.line_count)
                OR (q.quotation_status = 'PARTIALLY_ACCEPTED' AND s.awarded BETWEEN 1 AND s.line_count - 1)
                OR (q.quotation_status NOT IN ('ACCEPTED', 'PARTIALLY_ACCEPTED') AND s.awarded = 0))""")]
    found += [{**r, "problem": "RFQ line status vs awarded quotation line"} for r in ctx.rows("""
        SELECT l.rfq_line_item_id, l.line_status, h.rfq_status, a.awarded FROM rfq_line_items l
        JOIN rfq_headers h ON h.rfq_id = l.rfq_id
        LEFT JOIN (SELECT rfq_line_item_id, COUNT(*) AS awarded FROM quotation_line_items WHERE is_awarded
                   GROUP BY rfq_line_item_id) a ON a.rfq_line_item_id = l.rfq_line_item_id
        WHERE NOT ((l.line_status = 'AWARDED' AND a.awarded IS NOT NULL)
                OR (l.line_status = 'NOT_AWARDED' AND a.awarded IS NULL
                    AND h.rfq_status IN ('AWARDED', 'PARTIALLY_AWARDED', 'NO_AWARD'))
                OR (l.line_status = 'OPEN' AND a.awarded IS NULL AND h.rfq_status IN ('DRAFT', 'ISSUED', 'UNDER_EVALUATION'))
                OR (l.line_status = 'CANCELLED' AND a.awarded IS NULL AND h.rfq_status = 'CANCELLED'))""")]
    found += [{**r, "problem": "RFQ status vs line awards"} for r in ctx.rows("""
        SELECT h.rfq_id, h.rfq_status, s.awarded, s.line_count FROM rfq_headers h
        JOIN (SELECT rfq_id, SUM(line_status = 'AWARDED') AS awarded, COUNT(*) AS line_count FROM rfq_line_items
              GROUP BY rfq_id) s ON s.rfq_id = h.rfq_id
        WHERE (h.rfq_status = 'AWARDED' AND s.awarded <> s.line_count)
           OR (h.rfq_status = 'PARTIALLY_AWARDED' AND NOT s.awarded BETWEEN 1 AND s.line_count - 1)
           OR (h.rfq_status = 'NO_AWARD' AND s.awarded <> 0)""")]
    return found


@check("V-PR-06")
def quotation_lines_match_their_rfq(ctx: ValidationContext) -> Rows:
    """Quotation lines answer lines of the quotation's own RFQ, never quote more than requested, and the
    quotation is dated within the RFQ's response window."""
    found = [{**r, "problem": "RFQ line of another RFQ"} for r in ctx.rows("""
        SELECT q.quotation_id, ql.line_number FROM quotation_headers q
        JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id
        JOIN quotation_line_items ql ON ql.quotation_id = q.quotation_id
        JOIN rfq_line_items rl ON rl.rfq_line_item_id = ql.rfq_line_item_id
        WHERE rl.rfq_id <> rs.rfq_id""")]
    found += [{**r, "problem": "quoted more than requested"} for r in ctx.rows("""
        SELECT ql.quotation_id, ql.line_number, ql.quoted_quantity, rl.requested_quantity FROM quotation_line_items ql
        JOIN rfq_line_items rl ON rl.rfq_line_item_id = ql.rfq_line_item_id WHERE ql.quoted_quantity > rl.requested_quantity""")]
    found += [{**r, "problem": "quotation outside the response window"} for r in ctx.rows("""
        SELECT q.quotation_id, q.quotation_date, h.rfq_date, h.response_due_date FROM quotation_headers q
        JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id JOIN rfq_headers h ON h.rfq_id = rs.rfq_id
        WHERE q.quotation_date < h.rfq_date OR q.quotation_date > h.response_due_date""")]
    return found


@check("V-PR-07")
def award_purchase_orders_follow_the_quotation(ctx: ValidationContext) -> Rows:
    """RFQ_AWARD POs: supplier = quotation supplier; lines map 1:1 to the quotation's awarded lines with the
    same product, price and discount, ordering no more than quoted; PO dated on/after the award. The GST rate
    is the one in force on the PO date (V-PR-01), which differs from the quotation's after a rate change.
    Direct reorders reference no quotation line."""
    found = [{**r, "problem": "supplier ≠ quotation supplier"} for r in ctx.rows("""
        SELECT h.purchase_order_id, h.supplier_id, rs.supplier_id AS quoted_by FROM purchase_order_headers h
        JOIN quotation_headers q ON q.quotation_id = h.quotation_id JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id
        WHERE h.supplier_id <> rs.supplier_id""")]
    found += [{**r, "problem": "PO dated before the award"} for r in ctx.rows("""
        SELECT h.purchase_order_id, h.po_date, r.awarded_at FROM purchase_order_headers h
        JOIN quotation_headers q ON q.quotation_id = h.quotation_id JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id
        JOIN rfq_headers r ON r.rfq_id = rs.rfq_id
        WHERE r.awarded_at IS NULL OR h.po_date < DATE(r.awarded_at)""")]
    found += [{**r, "problem": "PO line differs from its awarded quotation line"} for r in ctx.rows("""
        SELECT l.purchase_order_id, l.line_number FROM purchase_order_line_items l
        JOIN purchase_order_headers h ON h.purchase_order_id = l.purchase_order_id
        LEFT JOIN quotation_line_items ql ON ql.quotation_line_item_id = l.quotation_line_item_id
        LEFT JOIN rfq_line_items rl ON rl.rfq_line_item_id = ql.rfq_line_item_id
        WHERE (h.po_source = 'DIRECT_REORDER' AND l.quotation_line_item_id IS NOT NULL)
           OR (h.po_source = 'RFQ_AWARD' AND (ql.quotation_line_item_id IS NULL OR ql.quotation_id <> h.quotation_id
               OR NOT ql.is_awarded OR rl.product_id <> l.product_id OR ql.unit_price <> l.unit_price
               OR ql.discount_percent <> l.discount_percent
               OR l.ordered_quantity > ql.quoted_quantity))""")]
    found += [{**r, "problem": "awarded quotation line without a PO line"} for r in ctx.rows("""
        SELECT ql.quotation_id, ql.line_number FROM quotation_line_items ql
        JOIN purchase_order_headers h ON h.quotation_id = ql.quotation_id
        WHERE ql.is_awarded AND NOT EXISTS (SELECT 1 FROM purchase_order_line_items l
                                            WHERE l.quotation_line_item_id = ql.quotation_line_item_id)""")]
    found += [{**r, "problem": "awarded quotation without a PO"} for r in ctx.rows("""
        SELECT q.quotation_id FROM quotation_headers q
        WHERE q.quotation_status IN ('ACCEPTED', 'PARTIALLY_ACCEPTED')
          AND NOT EXISTS (SELECT 1 FROM purchase_order_headers h WHERE h.quotation_id = q.quotation_id)""")]
    return found


@check("V-PR-08")
def po_line_counters_equal_their_sources(ctx: ValidationContext) -> Rows:
    """★ PO line counters = Σ GRN lines (received), Σ inspections (accepted / rejected), Σ supplier invoice
    lines (invoiced)."""
    return ctx.rows("""
        SELECT l.purchase_order_line_item_id, l.quantity_received, COALESCE(g.received, 0) AS grn_received,
               l.quantity_accepted, COALESCE(g.accepted, 0) AS inspected_accepted,
               l.quantity_rejected, COALESCE(g.rejected, 0) AS inspected_rejected,
               l.quantity_invoiced, COALESCE(i.invoiced, 0) AS invoice_lines
        FROM purchase_order_line_items l
        LEFT JOIN (SELECT gl.purchase_order_line_item_id, SUM(gl.quantity_received) AS received,
                          SUM(gi.quantity_accepted) AS accepted, SUM(gi.quantity_rejected) AS rejected
                   FROM goods_receipt_line_items gl JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
                   LEFT JOIN goods_inspections gi ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
                   WHERE g.grn_status <> 'CANCELLED' GROUP BY gl.purchase_order_line_item_id) g
               ON g.purchase_order_line_item_id = l.purchase_order_line_item_id
        LEFT JOIN (SELECT sl.purchase_order_line_item_id, SUM(sl.invoiced_quantity) AS invoiced
                   FROM supplier_invoice_line_items sl JOIN supplier_invoices s ON s.supplier_invoice_id = sl.supplier_invoice_id
                   WHERE s.invoice_status <> 'CANCELLED' GROUP BY sl.purchase_order_line_item_id) i
               ON i.purchase_order_line_item_id = l.purchase_order_line_item_id
        WHERE l.quantity_received <> COALESCE(g.received, 0) OR l.quantity_accepted <> COALESCE(g.accepted, 0)
           OR l.quantity_rejected <> COALESCE(g.rejected, 0) OR l.quantity_invoiced <> COALESCE(i.invoiced, 0)""")


# Receipt-driven PO statuses (1C §3): what the header and line statuses must be, given the line quantities.
_RECEIVING_PO_STATUSES = "('SENT', 'PARTIALLY_RECEIVED', 'RECEIVED')"


@check("V-PR-09")
def receipts_follow_the_po(ctx: ValidationContext) -> Rows:
    """GRN dated on/after the PO date and received after the PO was SENT; only approved-and-later POs receive
    goods; PO header and line statuses match their quantities."""
    found = [{**r, "problem": "received before the PO was sent"} for r in ctx.rows("""
        SELECT g.goods_receipt_id, g.received_at, h.po_date, s.sent_at, h.po_status FROM goods_receipts g
        JOIN purchase_order_headers h ON h.purchase_order_id = g.purchase_order_id
        LEFT JOIN (SELECT purchase_order_id, MIN(changed_at) AS sent_at FROM purchase_order_status_history
                   WHERE to_status = 'SENT' GROUP BY purchase_order_id) s ON s.purchase_order_id = g.purchase_order_id
        WHERE g.receipt_date < h.po_date OR s.sent_at IS NULL OR g.received_at < s.sent_at
           OR h.po_status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SENT', 'CANCELLED')""")]
    found += [{**r, "problem": "line status doesn't match its quantities"} for r in ctx.rows("""
        SELECT l.purchase_order_line_item_id, l.line_status, l.quantity_received, l.quantity_accepted, l.quantity_open
        FROM purchase_order_line_items l JOIN purchase_order_headers h ON h.purchase_order_id = l.purchase_order_id
        WHERE l.line_status <> CASE
            WHEN h.po_status = 'CLOSED' THEN 'CLOSED'
            WHEN l.quantity_open = 0 AND l.quantity_accepted > 0 THEN 'RECEIVED'
            WHEN l.quantity_open = 0 THEN 'CANCELLED'
            WHEN l.quantity_received > 0 THEN 'PARTIALLY_RECEIVED'
            ELSE 'OPEN' END""")]
    found += [{**r, "problem": "PO status doesn't match its line quantities"} for r in ctx.rows(f"""
        SELECT h.purchase_order_id, h.po_status, s.still_open, s.received FROM purchase_order_headers h
        JOIN (SELECT purchase_order_id, SUM(quantity_open > 0) AS still_open, SUM(quantity_received) AS received
              FROM purchase_order_line_items GROUP BY purchase_order_id) s ON s.purchase_order_id = h.purchase_order_id
        WHERE (h.po_status IN {_RECEIVING_PO_STATUSES} AND h.po_status <> CASE
                   WHEN s.still_open = 0 THEN 'RECEIVED' WHEN s.received > 0 THEN 'PARTIALLY_RECEIVED' ELSE 'SENT' END)
           OR (h.po_status IN ('SHORT_CLOSED', 'CANCELLED') AND s.still_open > 0)
           OR (h.po_status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'CANCELLED') AND s.received > 0)""")]
    return found


@check("V-PR-10")
def grn_lines_belong_to_the_grn(ctx: ValidationContext) -> Rows:
    """GRN lines reference lines of the GRN's own PO; receiving locations are RECEIVING locations of the GRN
    warehouse."""
    found = [{**r, "problem": "PO line of another PO"} for r in ctx.rows("""
        SELECT gl.goods_receipt_line_item_id FROM goods_receipt_line_items gl
        JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
        JOIN purchase_order_line_items l ON l.purchase_order_line_item_id = gl.purchase_order_line_item_id
        WHERE l.purchase_order_id <> g.purchase_order_id""")]
    found += [{**r, "problem": "receiving location outside the GRN warehouse's RECEIVING zone"} for r in ctx.rows("""
        SELECT gl.goods_receipt_line_item_id, wl.warehouse_id, wl.zone FROM goods_receipt_line_items gl
        JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
        JOIN warehouse_locations wl ON wl.warehouse_location_id = gl.receiving_location_id
        WHERE wl.warehouse_id <> g.warehouse_id OR wl.zone <> 'RECEIVING'""")]
    return found


@check("V-PR-11")
def inspections_split_the_receipt(ctx: ValidationContext) -> Rows:
    """★ Every GRN line of an INSPECTED GRN has its inspection with accepted + rejected = received; the
    PURCHASE_RECEIPT ledger quantity for the GRN line = accepted (rejected goods have no ledger row)."""
    return ctx.rows("""
        SELECT gl.goods_receipt_line_item_id, g.grn_status, gl.quantity_received, gi.quantity_accepted,
               gi.quantity_rejected, COALESCE(t.qty, 0) AS ledger_qty, COALESCE(t.rows_, 0) AS ledger_rows
        FROM goods_receipt_line_items gl JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
        LEFT JOIN goods_inspections gi ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
        LEFT JOIN (SELECT goods_receipt_line_item_id, SUM(quantity) AS qty, COUNT(*) AS rows_ FROM inventory_transactions
                   WHERE transaction_type = 'PURCHASE_RECEIPT' GROUP BY goods_receipt_line_item_id) t
               ON t.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
        WHERE (g.grn_status = 'INSPECTED' AND gi.goods_inspection_id IS NULL)
           OR (g.grn_status = 'RECEIVED' AND gi.goods_inspection_id IS NOT NULL)
           OR gi.quantity_accepted + gi.quantity_rejected <> gl.quantity_received
           OR COALESCE(t.qty, 0) <> COALESCE(gi.quantity_accepted, 0)
           OR (COALESCE(gi.quantity_accepted, 0) = 0 AND t.rows_ IS NOT NULL)""")


@check("V-PR-12")
def supplier_invoices_match_po_and_ap(ctx: ValidationContext) -> Rows:
    """★ Invoice supplier = PO supplier; bill-to GSTIN = the PO warehouse's GSTIN; cumulative invoiced ≤ accepted
    per PO line; every APPROVED invoice has its AP item for the invoice total (and no other invoice has one)."""
    found = [{**r, "problem": "supplier or bill-to GSTIN differs from the PO"} for r in ctx.rows("""
        SELECT i.supplier_invoice_id, i.supplier_id, h.supplier_id AS po_supplier, i.company_gst_registration_id,
               w.company_gst_registration_id AS po_gstin
        FROM supplier_invoices i JOIN purchase_order_headers h ON h.purchase_order_id = i.purchase_order_id
        JOIN warehouses w ON w.warehouse_id = h.delivery_warehouse_id
        WHERE i.supplier_id <> h.supplier_id OR i.company_gst_registration_id <> w.company_gst_registration_id""")]
    found += [{**r, "problem": "invoiced more than accepted"} for r in ctx.rows("""
        SELECT l.purchase_order_line_item_id, l.quantity_accepted, SUM(sl.invoiced_quantity) AS invoiced
        FROM purchase_order_line_items l JOIN supplier_invoice_line_items sl
             ON sl.purchase_order_line_item_id = l.purchase_order_line_item_id
        JOIN supplier_invoices i ON i.supplier_invoice_id = sl.supplier_invoice_id AND i.invoice_status <> 'CANCELLED'
        GROUP BY l.purchase_order_line_item_id, l.quantity_accepted HAVING SUM(sl.invoiced_quantity) > l.quantity_accepted""")]
    found += [{**r, "problem": "AP item missing, extra or with another amount"} for r in ctx.rows("""
        SELECT i.supplier_invoice_id, i.invoice_status, i.total_amount, a.original_amount FROM supplier_invoices i
        LEFT JOIN accounts_payable a ON a.supplier_invoice_id = i.supplier_invoice_id
        WHERE (i.invoice_status = 'APPROVED') <> (a.accounts_payable_id IS NOT NULL)
           OR a.original_amount <> i.total_amount OR a.document_date <> i.invoice_date OR a.due_date <> i.due_date""")]
    return found


@check("V-PR-13")
def supplier_invoice_lines_match_the_po(ctx: ValidationContext) -> Rows:
    """Invoice lines reference lines of the invoice's PO with the PO's price snapshot and GST rate; the match
    status says PRICE_VARIANCE exactly when a billed price differs from the PO price."""
    found = [{**r, "problem": "line differs from its PO line"} for r in ctx.rows("""
        SELECT sl.supplier_invoice_id, sl.line_number FROM supplier_invoice_line_items sl
        JOIN supplier_invoices i ON i.supplier_invoice_id = sl.supplier_invoice_id
        JOIN purchase_order_line_items l ON l.purchase_order_line_item_id = sl.purchase_order_line_item_id
        WHERE l.purchase_order_id <> i.purchase_order_id OR sl.po_unit_price <> l.unit_price OR sl.gst_rate <> l.gst_rate""")]
    found += [{**r, "problem": "match status vs billed prices"} for r in ctx.rows("""
        SELECT i.supplier_invoice_id, i.match_status, s.differing FROM supplier_invoices i
        JOIN (SELECT supplier_invoice_id, SUM(unit_price <> po_unit_price) AS differing FROM supplier_invoice_line_items
              GROUP BY supplier_invoice_id) s ON s.supplier_invoice_id = i.supplier_invoice_id
        WHERE i.match_status <> 'QUANTITY_HOLD' AND (i.match_status = 'PRICE_VARIANCE') <> (s.differing > 0)""")]
    return found


# V-PR-14: (table, approver column, approval date, approved amount or None).
APPROVALS = (
    ("purchase_order_headers", "approved_by_employee_id", "DATE(approved_at)", "total_amount"),
    ("supplier_invoices", "approved_by_employee_id", "DATE(approved_at)", "total_amount"),
    ("supplier_payments", "approved_by_employee_id", "payment_date", "amount"),
    ("stock_adjustments", "approved_by_employee_id", "adjustment_date", None),
)


@check("V-PR-14")
def approvers_active_and_within_limit(ctx: ValidationContext) -> Rows:
    """Approvers of POs, supplier invoices, supplier payments and stock adjustments were ACTIVE (hired, not yet
    left) on the approval date and their approval limit covers the amount."""
    found: Rows = []
    for table, column, on, amount in APPROVALS:
        found += [{**r, "table": table} for r in ctx.rows(f"""
            SELECT d.{column} AS employee_id, {on} AS approved_on, e.hire_date, e.termination_date, e.approval_limit,
                   {f"d.{amount}" if amount else "NULL"} AS amount
            FROM {table} d JOIN employees e ON e.employee_id = d.{column}
            WHERE {on} < e.hire_date OR (e.termination_date IS NOT NULL AND {on} > e.termination_date)
               {f"OR d.{amount} > e.approval_limit" if amount else ""}""")]
    return found
