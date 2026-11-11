"""Fulfilment and invoicing rules (1D §9: V-SO-03, V-SO-04, V-SH-*, V-IV-*). Each check returns the violating rows.

Step 3.9a implements the dispatch and invoicing side: ★ V-SO-03, ★ V-SO-04, V-SH-01, V-SH-02, V-IV-01, V-IV-02 and
V-IV-05. Carrier events (V-SH-03, V-SH-04) come with deliveries (3.9b), credit notes (V-IV-03, V-IV-04) with RTOs
and returns, freight bills (V-SH-05) with expenses.
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.checks.procurement import status_chain_problems
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

# Shipment lifecycle (1D §4, R11): the warehouse steps, the carrier's, failed attempts and returns to origin;
# cancellation before IN_TRANSIT (after PICKED_UP only under D40).
SHIPMENT_FLOW = ("PENDING", "PICKING", "PICKED", "PACKING", "PACKED", "READY_FOR_PICKUP", "PICKED_UP", "IN_TRANSIT",
                 "OUT_FOR_DELIVERY", "DELIVERED")
SHIPMENT_TRANSITIONS: set[tuple[str | None, str]] = {(None, "PENDING")} | set(zip(SHIPMENT_FLOW, SHIPMENT_FLOW[1:])) | {
    ("OUT_FOR_DELIVERY", "DELIVERY_FAILED"), ("DELIVERY_FAILED", "OUT_FOR_DELIVERY"),
    ("DELIVERY_FAILED", "RETURNED_TO_SENDER"),
} | {(status, "CANCELLED") for status in SHIPMENT_FLOW[:7]}

# Dispatched, not cancelled: the shipments whose lines count as shipped.
LIVE_SHIPMENT = "s.dispatched_at IS NOT NULL AND s.shipment_status <> 'CANCELLED'"


@check("V-SO-03")
def order_line_counters(ctx: ValidationContext) -> Rows:
    """★ Order-line counters (D32): shipped = Σ lines of dispatched, not cancelled shipments + handed-over invoice
    quantity; delivered = Σ lines of DELIVERED shipments + handed-over quantity; invoiced = Σ PRODUCT lines of
    ISSUED invoices; RTO = Σ lines of RETURNED_TO_SENDER shipments; return requested = Σ approved return quantity."""
    return ctx.rows(f"""
        SELECT l.order_line_item_id, l.quantity_shipped, COALESCE(s.shipped, 0) + COALESCE(v.handed, 0) AS shipped,
               l.quantity_delivered, COALESCE(s.delivered, 0) + COALESCE(v.handed, 0) AS delivered,
               l.quantity_invoiced, COALESCE(v.invoiced, 0) AS invoiced, l.quantity_rto, COALESCE(s.rto, 0) AS rto,
               l.quantity_return_requested, COALESCE(r.approved, 0) AS return_requested
        FROM order_line_items l
        LEFT JOIN (SELECT sl.order_line_item_id, SUM(sl.quantity_shipped) AS shipped,
                          SUM(IF(s.shipment_status = 'DELIVERED', sl.quantity_shipped, 0)) AS delivered,
                          SUM(IF(s.shipment_status = 'RETURNED_TO_SENDER', sl.quantity_shipped, 0)) AS rto
                   FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
                   WHERE {LIVE_SHIPMENT} GROUP BY sl.order_line_item_id) s ON s.order_line_item_id = l.order_line_item_id
        LEFT JOIN (SELECT il.order_line_item_id, SUM(il.quantity) AS invoiced,
                          SUM(IF(i.shipment_id IS NULL, il.quantity, 0)) AS handed
                   FROM invoice_line_items il JOIN invoices i ON i.invoice_id = il.invoice_id
                   WHERE i.invoice_status = 'ISSUED' AND il.line_type = 'PRODUCT'
                   GROUP BY il.order_line_item_id) v ON v.order_line_item_id = l.order_line_item_id
        LEFT JOIN (SELECT order_line_item_id, SUM(quantity_approved) AS approved FROM sales_return_line_items
                   GROUP BY order_line_item_id) r ON r.order_line_item_id = l.order_line_item_id
        WHERE l.quantity_shipped <> COALESCE(s.shipped, 0) + COALESCE(v.handed, 0)
           OR l.quantity_delivered <> COALESCE(s.delivered, 0) + COALESCE(v.handed, 0)
           OR l.quantity_invoiced <> COALESCE(v.invoiced, 0) OR l.quantity_rto <> COALESCE(s.rto, 0)
           OR l.quantity_return_requested <> COALESCE(r.approved, 0)""")


@check("V-SO-04")
def shipped_quantity_invoiced_once(ctx: ValidationContext) -> Rows:
    """★ Orders vs invoices: every line of a dispatched shipment is on exactly one ISSUED invoice line with its
    quantity; every ISSUED invoice of a shipped order bills a dispatched shipment, a handover invoice belongs to
    a store sale; per order line, Σ invoiced = Σ shipped or handed over."""
    found = [{**r, "problem": "dispatched shipment line not invoiced exactly once"} for r in ctx.rows(f"""
        SELECT sl.shipment_line_item_id, sl.quantity_shipped, COUNT(il.invoice_line_item_id) AS invoice_lines,
               SUM(il.quantity) AS invoiced
        FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
        LEFT JOIN invoice_line_items il ON il.shipment_line_item_id = sl.shipment_line_item_id
             AND il.invoice_id IN (SELECT invoice_id FROM invoices WHERE invoice_status = 'ISSUED')
        WHERE {LIVE_SHIPMENT}
        GROUP BY sl.shipment_line_item_id, sl.quantity_shipped
        HAVING COUNT(il.invoice_line_item_id) <> 1 OR SUM(il.quantity) <> sl.quantity_shipped
               OR MAX(il.order_line_item_id) <> MIN(sl.order_line_item_id)""")]
    found += [{**r, "problem": "invoice without a dispatch or a store handover"} for r in ctx.rows("""
        SELECT i.invoice_id, i.shipment_id, o.fulfilment_type, s.dispatched_at FROM invoices i
        JOIN orders o ON o.order_id = i.order_id LEFT JOIN shipments s ON s.shipment_id = i.shipment_id
        WHERE (o.fulfilment_type = 'SHIP') <> (i.shipment_id IS NOT NULL)
           OR (i.shipment_id IS NOT NULL AND s.dispatched_at IS NULL)
           OR (i.invoice_status = 'ISSUED' AND s.shipment_status = 'CANCELLED')""")]
    found += [{**r, "problem": "invoiced ≠ shipped or handed over"} for r in ctx.rows("""
        SELECT l.order_line_item_id, l.quantity_shipped, COALESCE(v.invoiced, 0) AS invoiced FROM order_line_items l
        LEFT JOIN (SELECT il.order_line_item_id, SUM(il.quantity) AS invoiced FROM invoice_line_items il
                   JOIN invoices i ON i.invoice_id = il.invoice_id
                   WHERE i.invoice_status = 'ISSUED' AND il.line_type = 'PRODUCT' GROUP BY il.order_line_item_id) v
               ON v.order_line_item_id = l.order_line_item_id
        WHERE l.quantity_shipped <> COALESCE(v.invoiced, 0)""")]
    return found


@check("V-SH-01")
def shipment_timeline(ctx: ValidationContext) -> Rows:
    """Shipment histories are continuous chains of allowed transitions ending at the shipment's status, in time
    order; created / dispatched / delivered times = the PENDING / PICKED_UP / DELIVERED entries; a shipment is
    for a shipped order and dispatched after the order was confirmed; delivery after dispatch."""
    found = status_chain_problems(ctx, "shipment", "shipments", "shipment_id", "shipment_status", SHIPMENT_TRANSITIONS)
    found += [{**r, "problem": "history out of time order"} for r in ctx.rows("""
        SELECT shipment_id, shipment_status_history_id, changed_at, previous_at FROM (
            SELECT shipment_id, shipment_status_history_id, changed_at,
                   LAG(changed_at) OVER (PARTITION BY shipment_id ORDER BY shipment_status_history_id) AS previous_at
            FROM shipment_status_history) h
        WHERE changed_at < previous_at""")]
    found += [{**r, "problem": "timestamps differ from the status history"} for r in ctx.rows("""
        SELECT s.shipment_id, s.created_at, s.dispatched_at, s.delivered_at, h.pending_at, h.picked_up_at, h.delivered_h
        FROM shipments s JOIN (
            SELECT shipment_id, MIN(IF(to_status = 'PENDING', changed_at, NULL)) AS pending_at,
                   MIN(IF(to_status = 'PICKED_UP', changed_at, NULL)) AS picked_up_at,
                   MIN(IF(to_status = 'DELIVERED', changed_at, NULL)) AS delivered_h
            FROM shipment_status_history GROUP BY shipment_id) h ON h.shipment_id = s.shipment_id
        WHERE s.created_at <> h.pending_at OR NOT (s.dispatched_at <=> h.picked_up_at)
           OR NOT (s.delivered_at <=> h.delivered_h)""")]
    found += [{**r, "problem": "shipment for an order that isn't shipped, or dispatched before confirmation"}
              for r in ctx.rows("""
        SELECT s.shipment_id, o.order_id, o.fulfilment_type, s.dispatched_at, c.confirmed_at FROM shipments s
        JOIN orders o ON o.order_id = s.order_id
        LEFT JOIN (SELECT order_id, MIN(changed_at) AS confirmed_at FROM order_status_history
                   WHERE to_status = 'CONFIRMED' GROUP BY order_id) c ON c.order_id = o.order_id
        WHERE o.fulfilment_type <> 'SHIP' OR s.shipping_address_id <> o.shipping_address_id
           OR (s.dispatched_at IS NOT NULL AND (c.confirmed_at IS NULL OR s.dispatched_at < c.confirmed_at
                                                OR s.created_at < c.confirmed_at))""")]
    return found


@check("V-SH-02")
def dispatched_lines_issued_and_invoiced(ctx: ValidationContext) -> Rows:
    """Every dispatched shipment line has exactly one SALES_ISSUE ledger row (−quantity, the shipment's warehouse,
    the line's batch and product, at the dispatch time; a cancelled dispatch adds its reversal) and one invoice
    line; a shipment cancelled before dispatch has neither."""
    found = [{**r, "problem": "issue row missing, duplicated or different from the shipment line"} for r in ctx.rows("""
        SELECT sl.shipment_line_item_id, COUNT(t.inventory_transaction_id) AS issues FROM shipment_line_items sl
        JOIN shipments s ON s.shipment_id = sl.shipment_id
        JOIN order_line_items l ON l.order_line_item_id = sl.order_line_item_id
        LEFT JOIN inventory_transactions t ON t.shipment_line_item_id = sl.shipment_line_item_id
             AND t.transaction_type = 'SALES_ISSUE' AND t.reversal_of_inventory_transaction_id IS NULL
        WHERE s.dispatched_at IS NOT NULL
        GROUP BY sl.shipment_line_item_id
        HAVING COUNT(t.inventory_transaction_id) <> 1
            OR MAX(t.quantity) <> -MAX(sl.quantity_shipped) OR MAX(t.warehouse_id) <> MAX(s.warehouse_id)
            OR MAX(t.product_id) <> MAX(l.product_id) OR MAX(t.transaction_at) <> MAX(s.dispatched_at)
            OR NOT (MAX(t.inventory_batch_id) <=> MAX(sl.inventory_batch_id))""")]
    found += [{**r, "problem": "dispatched shipment line without an invoice line"} for r in ctx.rows("""
        SELECT sl.shipment_line_item_id FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
        LEFT JOIN invoice_line_items il ON il.shipment_line_item_id = sl.shipment_line_item_id
        WHERE s.dispatched_at IS NOT NULL AND il.invoice_line_item_id IS NULL""")]
    found += [{**r, "problem": "shipment cancelled before dispatch has stock or invoice rows"} for r in ctx.rows("""
        SELECT sl.shipment_line_item_id FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
        WHERE s.dispatched_at IS NULL
          AND (EXISTS (SELECT 1 FROM inventory_transactions t WHERE t.shipment_line_item_id = sl.shipment_line_item_id)
            OR EXISTS (SELECT 1 FROM invoice_line_items il WHERE il.shipment_line_item_id = sl.shipment_line_item_id))""")]
    return found


@check("V-IV-01")
def invoice_consistency(ctx: ValidationContext) -> Rows:
    """Invoice ↔ order ↔ shipment ↔ warehouse ↔ GSTIN (the composite FKs) plus what they can't express: issued at
    the dispatch (or handover); billing / shipping address and payment term = the order's; place of supply = the
    ship-to state, or the store's state for a handover; due date from the term; customer name and GSTIN = the
    customer master; e-invoice reference exactly for GST-registered buyers (D46)."""
    return ctx.rows("""
        SELECT i.invoice_id,
               CASE WHEN s.shipment_id IS NOT NULL AND i.issued_at <> s.dispatched_at THEN 'not issued at the dispatch'
                    WHEN i.billing_address_id <> o.billing_address_id
                      OR NOT (i.shipping_address_id <=> o.shipping_address_id) THEN 'addresses differ from the order'
                    WHEN i.payment_term_id <> o.payment_term_id
                      OR i.due_date <> i.invoice_date + INTERVAL t.due_days DAY THEN 'payment term or due date'
                    WHEN i.place_of_supply_state_id <> COALESCE(a.state_id, w.state_id) THEN 'place of supply'
                    WHEN i.customer_name <> c.customer_name OR NOT (i.customer_gstin <=> c.gstin)
                         THEN 'customer snapshot differs from the master'
                    ELSE 'e-invoice reference iff registered buyer' END AS problem
        FROM invoices i JOIN orders o ON o.order_id = i.order_id JOIN customers c ON c.customer_id = i.customer_id
        JOIN payment_terms t ON t.payment_term_id = i.payment_term_id JOIN warehouses w ON w.warehouse_id = i.warehouse_id
        LEFT JOIN shipments s ON s.shipment_id = i.shipment_id
        LEFT JOIN customer_addresses a ON a.customer_address_id = i.shipping_address_id
        WHERE (s.shipment_id IS NOT NULL AND i.issued_at <> s.dispatched_at)
           OR i.billing_address_id <> o.billing_address_id OR NOT (i.shipping_address_id <=> o.shipping_address_id)
           OR i.payment_term_id <> o.payment_term_id OR i.due_date <> i.invoice_date + INTERVAL t.due_days DAY
           OR i.place_of_supply_state_id <> COALESCE(a.state_id, w.state_id)
           OR i.customer_name <> c.customer_name OR NOT (i.customer_gstin <=> c.gstin)
           OR (i.irn IS NULL) <> (i.customer_gstin IS NULL)""")


@check("V-IV-02")
def invoice_lines(ctx: ValidationContext) -> Rows:
    """Product lines: the order line's product, HSN, price and discount (the GST-inclusive price for B2C, D79) at
    the product's GST rate on the invoice date; 1:1 with shipment lines for shipped orders, one per order line
    for a handover. Shipping: at most one SHIPPING_CHARGE line per order across its ISSUED invoices, for the
    order's charge, at the invoice's highest goods rate (D42), and none left unbilled once the order is invoiced."""
    found = [{**r, "problem": "product line differs from its order line or the invoice-date rate"} for r in ctx.rows("""
        SELECT il.invoice_line_item_id, il.unit_price, l.unit_price AS order_price, il.inclusive_unit_price,
               l.inclusive_unit_price AS order_inclusive, il.gst_rate, r.gst_rate AS rate_on_date
        FROM invoice_line_items il JOIN invoices i ON i.invoice_id = il.invoice_id
        JOIN order_line_items l ON l.order_line_item_id = il.order_line_item_id
        JOIN products p ON p.product_id = il.product_id
        LEFT JOIN product_tax_rates r ON r.product_id = il.product_id AND r.effective_from <= i.invoice_date
             AND (r.effective_to IS NULL OR r.effective_to >= i.invoice_date)
        WHERE il.line_type = 'PRODUCT'
          AND (il.hsn_code <> p.hsn_code OR il.discount_percent <> l.discount_percent OR r.gst_rate IS NULL
               OR il.gst_rate <> r.gst_rate OR NOT (il.inclusive_unit_price <=> l.inclusive_unit_price)
               OR (l.inclusive_unit_price IS NULL AND il.unit_price <> l.unit_price))""")]
    found += [{**r, "problem": "lines not 1:1 with shipment lines (shipped) or order lines (handover)"} for r in ctx.rows("""
        SELECT il.invoice_line_item_id, i.invoice_id, i.shipment_id, il.shipment_line_item_id FROM invoice_line_items il
        JOIN invoices i ON i.invoice_id = il.invoice_id
        LEFT JOIN shipment_line_items sl ON sl.shipment_line_item_id = il.shipment_line_item_id
        WHERE il.line_type = 'PRODUCT'
          AND ((i.shipment_id IS NOT NULL AND (sl.shipment_id IS NULL OR sl.shipment_id <> i.shipment_id
                                               OR sl.quantity_shipped <> il.quantity
                                               OR sl.order_line_item_id <> il.order_line_item_id))
            OR (i.shipment_id IS NULL AND il.shipment_line_item_id IS NOT NULL))
        UNION ALL
        SELECT MIN(il.invoice_line_item_id), il.invoice_id, NULL, NULL FROM invoice_line_items il
        JOIN invoices i ON i.invoice_id = il.invoice_id
        WHERE i.shipment_id IS NULL AND il.line_type = 'PRODUCT'
        GROUP BY il.invoice_id, il.order_line_item_id HAVING COUNT(*) > 1""")]
    found += [{**r, "problem": "shipping charge billed twice, wrongly, or not at all"} for r in ctx.rows("""
        SELECT o.order_id, o.shipping_charge_amount, COUNT(sc.invoice_line_item_id) AS shipping_lines,
               MIN(sc.taxable_amount) AS billed, MIN(sc.gst_rate) AS rate, MIN(g.top_rate) AS top_rate
        FROM orders o JOIN invoices i ON i.order_id = o.order_id AND i.invoice_status = 'ISSUED'
        LEFT JOIN invoice_line_items sc ON sc.invoice_id = i.invoice_id AND sc.line_type = 'SHIPPING_CHARGE'
        LEFT JOIN (SELECT invoice_id, MAX(gst_rate) AS top_rate FROM invoice_line_items WHERE line_type = 'PRODUCT'
                   GROUP BY invoice_id) g ON g.invoice_id = sc.invoice_id
        GROUP BY o.order_id, o.shipping_charge_amount
        HAVING COUNT(sc.invoice_line_item_id) <> (o.shipping_charge_amount > 0)
            OR MIN(sc.taxable_amount) <> o.shipping_charge_amount OR MIN(sc.gst_rate) <> MIN(g.top_rate)
            OR MIN(sc.hsn_code) <> '996812'""")]
    return found


@check("V-IV-05")
def invoice_numbers_and_cancellations(ctx: ValidationContext) -> Rows:
    """Invoice numbers: ≤ 16 characters, the issuing GSTIN's state and the invoice date's financial year in the
    prefix, unique per GSTIN per FY (D38; gap-free series: V-SY-01). Cancelled invoices: within 24 h of issue and
    before the shipment went IN_TRANSIT, and the shipment is cancelled too (D40)."""
    found = [{**r, "problem": "number doesn't carry the GSTIN's state and the financial year"} for r in ctx.rows("""
        SELECT i.invoice_id, i.invoice_number, s.state_code, i.invoice_date FROM invoices i
        JOIN company_gst_registrations g ON g.company_gst_registration_id = i.company_gst_registration_id
        JOIN states s ON s.state_id = g.state_id CROSS JOIN (SELECT fy_start_month FROM companies LIMIT 1) c
        WHERE CHAR_LENGTH(i.invoice_number) > 16
           OR LEFT(i.invoice_number, 7) <> CONCAT(s.state_code,
                  LPAD(MOD(YEAR(i.invoice_date) - (MONTH(i.invoice_date) < c.fy_start_month), 100), 2, '0'),
                  LPAD(MOD(YEAR(i.invoice_date) - (MONTH(i.invoice_date) < c.fy_start_month) + 1, 100), 2, '0'), '-')""")]
    found += [{**r, "problem": "cancelled too late, after IN_TRANSIT, or shipment still live"} for r in ctx.rows("""
        SELECT i.invoice_id, i.issued_at, i.cancelled_at, s.shipment_status, t.in_transit_at FROM invoices i
        LEFT JOIN shipments s ON s.shipment_id = i.shipment_id
        LEFT JOIN (SELECT shipment_id, MIN(changed_at) AS in_transit_at FROM shipment_status_history
                   WHERE to_status = 'IN_TRANSIT' GROUP BY shipment_id) t ON t.shipment_id = i.shipment_id
        WHERE i.invoice_status = 'CANCELLED'
          AND (i.cancelled_at > i.issued_at + INTERVAL 24 HOUR OR i.cancelled_at >= t.in_transit_at
               OR (s.shipment_id IS NOT NULL AND s.shipment_status <> 'CANCELLED'))""")]
    return found
