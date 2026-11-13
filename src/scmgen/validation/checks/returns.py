"""Returns rules (1D §9: V-RT-01 … V-RT-05). Each check returns the violating rows.

Step 3.11 implements V-RT-01, ★ V-RT-02, ★ V-RT-03, ★ V-RT-04 (refunds) and V-RT-05 (registration and pending
dispositions; the disposition ledger, P18 and adjustment-sourced entries with 3.12b).
V-RT-06 (returns to origin) lives with the carrier rules in ``fulfilment.py``.
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-RT-01")
def return_requests(ctx: ValidationContext) -> Rows:
    """Approved returns were requested within the category's return window of the delivery, for returnable
    products, after the delivery; rejected requests have nothing approved or received; a return's lines belong to
    its order."""
    found = [{**r, "problem": "approved outside the return window, for a non-returnable product or before delivery"}
             for r in ctx.rows("""
        SELECT r.sales_return_id, rl.line_number, r.requested_at, d.delivered_on, c.return_window_days, p.is_returnable
        FROM sales_returns r JOIN sales_return_line_items rl ON rl.sales_return_id = r.sales_return_id
        JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
        JOIN (SELECT i.invoice_id, COALESCE(s.delivered_at, i.issued_at) AS delivered_on FROM invoices i
              LEFT JOIN shipments s ON s.shipment_id = i.shipment_id) d ON d.invoice_id = il.invoice_id
        JOIN products p ON p.product_id = il.product_id
        JOIN product_categories c ON c.product_category_id = p.product_category_id
        WHERE r.return_status NOT IN ('REQUESTED', 'REJECTED', 'CANCELLED')
          AND (DATE(r.requested_at) > DATE(d.delivered_on) + INTERVAL c.return_window_days DAY OR NOT p.is_returnable
               OR r.requested_at < d.delivered_on)""")]
    found += [{**r, "problem": "rejected request with approved or received quantity"} for r in ctx.rows("""
        SELECT r.sales_return_id, rl.line_number FROM sales_returns r
        JOIN sales_return_line_items rl ON rl.sales_return_id = r.sales_return_id
        WHERE r.return_status = 'REJECTED' AND (rl.quantity_approved > 0 OR rl.quantity_received > 0)""")]
    found += [{**r, "problem": "return line of another order"} for r in ctx.rows("""
        SELECT r.sales_return_id, rl.line_number FROM sales_returns r
        JOIN sales_return_line_items rl ON rl.sales_return_id = r.sales_return_id
        JOIN order_line_items l ON l.order_line_item_id = rl.order_line_item_id
        WHERE l.order_id <> r.order_id""")]
    return found


@check("V-RT-02")
def returned_within_delivered(ctx: ValidationContext) -> Rows:
    """★ Approved return quantity ≤ delivered quantity per order line, and received ≤ invoiced per invoice line."""
    found = [{**r, "problem": "more approved for return than delivered"} for r in ctx.rows("""
        SELECT l.order_line_item_id, l.quantity_delivered, SUM(rl.quantity_approved) AS approved
        FROM order_line_items l JOIN sales_return_line_items rl ON rl.order_line_item_id = l.order_line_item_id
        GROUP BY l.order_line_item_id, l.quantity_delivered HAVING SUM(rl.quantity_approved) > l.quantity_delivered""")]
    found += [{**r, "problem": "more received than invoiced"} for r in ctx.rows("""
        SELECT il.invoice_line_item_id, il.quantity, SUM(rl.quantity_received) AS received
        FROM invoice_line_items il JOIN sales_return_line_items rl ON rl.invoice_line_item_id = il.invoice_line_item_id
        GROUP BY il.invoice_line_item_id, il.quantity HAVING SUM(rl.quantity_received) > il.quantity""")]
    return found


@check("V-RT-03")
def returned_goods_in_inventory(ctx: ValidationContext) -> Rows:
    """★ RETURN_RECEIPT = received quantity into QUARANTINE at the return warehouse, at the original issue cost;
    inspections move it out of QUARANTINE (−) into SELLABLE / DEFECTIVE (+) as recorded; a credited line was
    fully inspected (sellable + defective = received); defective quantities have their ``defective_stock`` row."""
    found = [{**r, "problem": "receipt row differs from the received line"} for r in ctx.rows("""
        SELECT rl.sales_return_line_item_id, t.quantity, rl.quantity_received, t.stock_status, t.warehouse_id
        FROM sales_return_line_items rl JOIN sales_returns r ON r.sales_return_id = rl.sales_return_id
        LEFT JOIN inventory_transactions t ON t.sales_return_line_item_id = rl.sales_return_line_item_id
             AND t.transaction_type = 'RETURN_RECEIPT'
        WHERE rl.quantity_received > 0
          AND (t.inventory_transaction_id IS NULL OR t.quantity <> rl.quantity_received OR t.stock_status <> 'QUARANTINE'
               OR t.warehouse_id <> r.return_warehouse_id OR t.transaction_at <> rl.received_at)""")]
    found += [{**r, "problem": "receipt not at the original issue cost"} for r in ctx.rows("""
        SELECT rl.sales_return_line_item_id, r.unit_cost,
               COALESCE((SELECT x.unit_cost FROM inventory_transactions x
                         WHERE x.shipment_line_item_id = il.shipment_line_item_id AND x.transaction_type = 'SALES_ISSUE'
                           AND x.reversal_of_inventory_transaction_id IS NULL ORDER BY x.inventory_transaction_id LIMIT 1),
                        (SELECT x.unit_cost FROM inventory_transactions x
                         WHERE x.invoice_line_item_id = il.invoice_line_item_id AND x.transaction_type = 'SALES_ISSUE'
                           AND x.reversal_of_inventory_transaction_id IS NULL ORDER BY x.inventory_transaction_id LIMIT 1))
                   AS issue_cost
        FROM sales_return_line_items rl JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
        JOIN inventory_transactions r
             ON r.sales_return_line_item_id = rl.sales_return_line_item_id AND r.transaction_type = 'RETURN_RECEIPT'
        HAVING issue_cost IS NULL OR unit_cost <> issue_cost""")]
    found += [{**r, "problem": "disposition rows differ from the inspection"} for r in ctx.rows("""
        SELECT i.return_inspection_id, i.quantity_sellable, i.quantity_defective,
               COALESCE(SUM(IF(t.stock_status = 'SELLABLE', t.quantity, 0)), 0) AS to_sellable,
               COALESCE(SUM(IF(t.stock_status = 'DEFECTIVE', t.quantity, 0)), 0) AS to_defective,
               COALESCE(SUM(IF(t.stock_status = 'QUARANTINE', t.quantity, 0)), 0) AS out_of_quarantine
        FROM return_inspections i LEFT JOIN inventory_transactions t ON t.return_inspection_id = i.return_inspection_id
             AND t.transaction_type = 'RETURN_DISPOSITION'
        GROUP BY i.return_inspection_id, i.quantity_sellable, i.quantity_defective
        HAVING to_sellable <> i.quantity_sellable OR to_defective <> i.quantity_defective
            OR out_of_quarantine <> -(i.quantity_sellable + i.quantity_defective)""")]
    found += [{**r, "problem": "credited line not fully inspected"} for r in ctx.rows("""
        SELECT rl.sales_return_line_item_id, rl.quantity_received, SUM(i.quantity_sellable + i.quantity_defective) AS decided
        FROM sales_return_line_items rl LEFT JOIN return_inspections i
             ON i.sales_return_line_item_id = rl.sales_return_line_item_id
        WHERE rl.line_status IN ('INSPECTED', 'CREDITED')
        GROUP BY rl.sales_return_line_item_id, rl.quantity_received
        HAVING COALESCE(SUM(i.quantity_sellable + i.quantity_defective), 0) <> rl.quantity_received""")]
    found += [{**r, "problem": "defective quantity without its register row"} for r in ctx.rows("""
        SELECT i.return_inspection_id, i.quantity_defective, d.quantity FROM return_inspections i
        LEFT JOIN defective_stock d ON d.return_inspection_id = i.return_inspection_id
        WHERE (i.quantity_defective > 0) <> (d.defective_stock_id IS NOT NULL) OR d.quantity <> i.quantity_defective""")]
    return found


@check("V-RT-05")
def defective_register(ctx: ValidationContext) -> Rows:
    """Defective stock: exactly one source (DB CHECK) whose warehouse and product it matches; completed
    dispositions have their DEFECTIVE_DISPOSITION rows; what is still pending stays in ``inventory.qty_defective``
    (Σ pending per warehouse × product ≤ qty_defective)."""
    found = [{**r, "problem": "register row doesn't match its return inspection"} for r in ctx.rows("""
        SELECT d.defective_stock_id, d.warehouse_id, r.return_warehouse_id, d.product_id, il.product_id AS line_product
        FROM defective_stock d JOIN return_inspections i ON i.return_inspection_id = d.return_inspection_id
        JOIN sales_return_line_items rl ON rl.sales_return_line_item_id = i.sales_return_line_item_id
        JOIN sales_returns r ON r.sales_return_id = rl.sales_return_id
        JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
        WHERE d.warehouse_id <> r.return_warehouse_id OR d.product_id <> il.product_id""")]
    found += [{**r, "problem": "register row doesn't match its adjustment line (handling damage)"} for r in ctx.rows("""
        SELECT d.defective_stock_id, d.quantity, l.quantity_change, l.stock_status, d.warehouse_id, a.warehouse_id AS adj_w
        FROM defective_stock d JOIN stock_adjustment_line_items l
             ON l.stock_adjustment_line_item_id = d.stock_adjustment_line_item_id
        JOIN stock_adjustments a ON a.stock_adjustment_id = l.stock_adjustment_id
        WHERE d.warehouse_id <> a.warehouse_id OR d.product_id <> l.product_id OR d.quantity <> l.quantity_change
           OR l.stock_status <> 'DEFECTIVE' OR NOT (d.inventory_batch_id <=> l.inventory_batch_id)""")]
    found += [{**r, "problem": "completed disposition's ledger rows don't match it"} for r in ctx.rows("""
        SELECT d.defective_stock_id, d.disposition, d.quantity,
               COALESCE(SUM(IF(t.stock_status = 'DEFECTIVE', t.quantity, 0)), 0) AS out_of_defective,
               COALESCE(SUM(IF(t.stock_status = 'SELLABLE', t.quantity, 0)), 0) AS to_sellable,
               COALESCE(SUM(t.value_change), 0) AS value, COUNT(t.inventory_transaction_id) AS rows_
        FROM defective_stock d LEFT JOIN inventory_transactions t ON t.defective_stock_id = d.defective_stock_id
             AND t.transaction_type = 'DEFECTIVE_DISPOSITION'
        WHERE d.disposition_status = 'COMPLETED'
        GROUP BY d.defective_stock_id, d.disposition, d.quantity
        HAVING rows_ = 0 OR out_of_defective <> -d.quantity
            OR (d.disposition = 'REPAIR' AND (to_sellable <> d.quantity OR value <> 0))
            OR (d.disposition <> 'REPAIR' AND to_sellable <> 0)""")]
    found += [{**r, "problem": "write-off disposition without its P18 journal of the written-off value"}
              for r in ctx.rows("""
        SELECT d.defective_stock_id, -SUM(t.value_change) AS value, j.total_debit
        FROM defective_stock d JOIN inventory_transactions t ON t.defective_stock_id = d.defective_stock_id
             AND t.transaction_type = 'DEFECTIVE_DISPOSITION'
        LEFT JOIN journal_entries j ON j.defective_stock_id = d.defective_stock_id
             AND j.journal_source = 'DEFECTIVE_DISPOSITION'
        WHERE d.disposition IN ('SCRAP', 'RETURN_TO_SUPPLIER')
        GROUP BY d.defective_stock_id, j.total_debit
        HAVING -SUM(t.value_change) > 0 AND NOT (j.total_debit <=> -SUM(t.value_change))""")]
    found += [{**r, "problem": "returned to a supplier that isn't an approved supplier of the product"}
              for r in ctx.rows("""
        SELECT d.defective_stock_id, d.supplier_id FROM defective_stock d
        LEFT JOIN supplier_products s ON s.supplier_id = d.supplier_id AND s.product_id = d.product_id
        WHERE d.disposition = 'RETURN_TO_SUPPLIER' AND s.supplier_product_id IS NULL""")]
    found += [{**r, "problem": "pending defective stock exceeds qty_defective"} for r in ctx.rows("""
        SELECT d.warehouse_id, d.product_id, SUM(d.quantity) AS pending, i.qty_defective FROM defective_stock d
        LEFT JOIN inventory i ON i.warehouse_id = d.warehouse_id AND i.product_id = d.product_id
        WHERE d.disposition_status = 'PENDING' GROUP BY d.warehouse_id, d.product_id, i.qty_defective
        HAVING SUM(d.quantity) > COALESCE(i.qty_defective, 0)""")]
    return found


@check("V-RT-04")
def refunds_within_credit_and_payment(ctx: ValidationContext) -> Rows:
    """★ Returns vs refunds: Σ COMPLETED and in-flight refunds per credit note ≤ the credit note total ≤ what the
    customer paid on the invoice (SUCCESS payments allocated to it) when refunded; a refund to source names a
    SUCCESS payment of the same customer; a refunded consumer credit is settled by its refund (D49)."""
    found = [{**r, "problem": "refunds exceed the credit note or what the customer paid"} for r in ctx.rows("""
        SELECT n.credit_note_id, n.total_amount, SUM(r.refund_amount) AS refunded, COALESCE(p.paid, 0) AS paid
        FROM credit_notes n JOIN refunds r ON r.credit_note_id = n.credit_note_id
             AND r.refund_status NOT IN ('FAILED', 'REJECTED')
        LEFT JOIN (SELECT x.invoice_id, SUM(x.allocated_amount) AS paid FROM customer_payment_allocations x
                   JOIN customer_payments y ON y.customer_payment_id = x.customer_payment_id
                   WHERE y.payment_status = 'SUCCESS' AND x.allocation_status = 'ACTIVE' GROUP BY x.invoice_id) p
               ON p.invoice_id = n.invoice_id
        GROUP BY n.credit_note_id, n.total_amount, p.paid
        HAVING SUM(r.refund_amount) > n.total_amount OR SUM(r.refund_amount) > COALESCE(p.paid, 0)""")]
    found += [{**r, "problem": "refund to a payment that isn't a SUCCESS payment of the customer"} for r in ctx.rows("""
        SELECT r.refund_id, y.customer_id, n.customer_id AS note_customer, y.payment_status FROM refunds r
        JOIN credit_notes n ON n.credit_note_id = r.credit_note_id
        JOIN customer_payments y ON y.customer_payment_id = r.customer_payment_id
        WHERE y.customer_id <> n.customer_id OR y.payment_status <> 'SUCCESS'""")]
    found += [{**r, "problem": "completed refund not allocated against its credit note"} for r in ctx.rows("""
        SELECT r.refund_id, r.refund_amount, x.allocated_amount FROM refunds r
        LEFT JOIN customer_payment_allocations x ON x.refund_id = r.refund_id AND x.allocation_status = 'ACTIVE'
        WHERE r.refund_status = 'COMPLETED'
          AND (x.customer_payment_allocation_id IS NULL OR x.allocated_amount <> r.refund_amount
               OR x.credit_note_id <> r.credit_note_id)""")]
    return found
