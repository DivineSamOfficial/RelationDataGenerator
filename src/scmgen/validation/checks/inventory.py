"""Inventory rules (1C §8, V-IN-*). Each check returns the violating rows.

Implemented: ★ V-IN-01, V-IN-02, V-IN-04 (opening stock, 3.6b); V-IN-05, V-IN-10 (receipts, 3.7b); ★ V-IN-03
(reservations, 3.8b); V-IN-09 and the sales issues of V-IN-05 (dispatch and handover, 3.9a); V-IN-07 and
★ V-IN-08 with the transfer sources of V-IN-05 (3.12a).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text

from scmgen.domain.costing import StockError, StockPosition
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-IN-01")
def balances_equal_the_ledger(ctx: ValidationContext) -> Rows:
    """★ For every warehouse × product × status: Σ ledger quantity = the inventory column; Σ value = stock_value."""
    return ctx.rows("""
        SELECT k.warehouse_id, k.product_id,
               COALESCE(i.qty_sellable, 0) AS qty_sellable, COALESCE(g.sellable, 0) AS ledger_sellable,
               COALESCE(i.qty_quarantine, 0) AS qty_quarantine, COALESCE(g.quarantine, 0) AS ledger_quarantine,
               COALESCE(i.qty_defective, 0) AS qty_defective, COALESCE(g.defective, 0) AS ledger_defective,
               COALESCE(i.stock_value, 0) AS stock_value, COALESCE(g.value, 0) AS ledger_value
        FROM (SELECT warehouse_id, product_id FROM inventory
              UNION SELECT warehouse_id, product_id FROM inventory_transactions) k
        LEFT JOIN inventory i ON i.warehouse_id = k.warehouse_id AND i.product_id = k.product_id
        LEFT JOIN (SELECT warehouse_id, product_id,
                          SUM(IF(stock_status = 'SELLABLE', quantity, 0)) AS sellable,
                          SUM(IF(stock_status = 'QUARANTINE', quantity, 0)) AS quarantine,
                          SUM(IF(stock_status = 'DEFECTIVE', quantity, 0)) AS defective,
                          SUM(value_change) AS value
                   FROM inventory_transactions GROUP BY warehouse_id, product_id) g
               ON g.warehouse_id = k.warehouse_id AND g.product_id = k.product_id
        WHERE COALESCE(i.qty_sellable, 0) <> COALESCE(g.sellable, 0)
           OR COALESCE(i.qty_quarantine, 0) <> COALESCE(g.quarantine, 0)
           OR COALESCE(i.qty_defective, 0) <> COALESCE(g.defective, 0)
           OR COALESCE(i.stock_value, 0) <> COALESCE(g.value, 0)""")


@check("V-IN-02")
def never_negative_in_time_order(ctx: ValidationContext) -> Rows:
    """Replaying the ledger in time order never takes a warehouse × product × status below zero."""
    return ctx.rows("""
        SELECT warehouse_id, product_id, stock_status, transaction_at, running
        FROM (SELECT warehouse_id, product_id, stock_status, transaction_at,
                     SUM(quantity) OVER (PARTITION BY warehouse_id, product_id, stock_status
                                         ORDER BY transaction_at, inventory_transaction_id) AS running
              FROM inventory_transactions) r
        WHERE running < 0""")


@check("V-IN-04")
def batches_consistent(ctx: ValidationContext) -> Rows:
    """Batch quantity per warehouse never negative over time; batch-tracked products always move with a batch;
    no expired batch is issued to a customer."""
    found = [{**r, "problem": "batch quantity below zero"} for r in ctx.rows("""
        SELECT inventory_batch_id, warehouse_id, transaction_at, running
        FROM (SELECT inventory_batch_id, warehouse_id, transaction_at,
                     SUM(quantity) OVER (PARTITION BY inventory_batch_id, warehouse_id
                                         ORDER BY transaction_at, inventory_transaction_id) AS running
              FROM inventory_transactions WHERE inventory_batch_id IS NOT NULL) r
        WHERE running < 0""")]
    found += [{**r, "problem": "batch-tracked product moved without a batch"} for r in ctx.rows("""
        SELECT t.inventory_transaction_id, t.product_id FROM inventory_transactions t
        JOIN products p ON p.product_id = t.product_id
        WHERE p.is_batch_tracked AND t.inventory_batch_id IS NULL""")]
    found += [{**r, "problem": "expired batch issued to a customer"} for r in ctx.rows("""
        SELECT t.inventory_transaction_id, b.batch_number, b.expiry_date FROM inventory_transactions t
        JOIN inventory_batches b ON b.inventory_batch_id = t.inventory_batch_id
        WHERE t.transaction_type = 'SALES_ISSUE' AND b.expiry_date < DATE(t.transaction_at)""")]
    return found


# V-IN-05 / V-IN-10: (ledger type, source column, source event SQL: id, business date, quantity it must move,
# one row per event?). Each process step adds its source events here; a type may have several sources.
LEDGER_SOURCES = [
    ("PURCHASE_RECEIPT", "goods_receipt_line_item_id", """
        SELECT gl.goods_receipt_line_item_id AS source_id, g.receipt_date AS business_date, gi.quantity_accepted AS qty
        FROM goods_receipt_line_items gl JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
        JOIN goods_inspections gi ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
        WHERE g.grn_status = 'INSPECTED' AND gi.quantity_accepted > 0""", True),
    ("ADJUSTMENT", "stock_adjustment_line_item_id", """
        SELECT l.stock_adjustment_line_item_id AS source_id, a.adjustment_date AS business_date, l.quantity_change AS qty
        FROM stock_adjustment_line_items l JOIN stock_adjustments a ON a.stock_adjustment_id = l.stock_adjustment_id
        WHERE a.adjustment_status = 'POSTED' AND l.quantity_change <> 0""", True),
    # Dispatch (step 3.9): one issue per shipment line (lines are per batch); a cancelled dispatch (D40) keeps its
    # issue and adds the reversal, which nets out.
    ("SALES_ISSUE", "shipment_line_item_id", """
        SELECT sl.shipment_line_item_id AS source_id, s.planned_dispatch_date AS business_date,
               IF(s.shipment_status = 'CANCELLED', 0, -sl.quantity_shipped) AS qty
        FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
        WHERE s.dispatched_at IS NOT NULL""", True),
    # Return to origin (3.9b, D41): every line of a RETURNED_TO_SENDER shipment comes back in one row.
    ("RTO_RECEIPT", "shipment_line_item_id", """
        SELECT sl.shipment_line_item_id AS source_id, DATE(s.rto_received_at) AS business_date,
               sl.quantity_shipped AS qty
        FROM shipment_line_items sl JOIN shipments s ON s.shipment_id = sl.shipment_id
        WHERE s.shipment_status = 'RETURNED_TO_SENDER'""", True),
    # Customer returns (3.11): the received quantity comes in once; each inspection moves its quantity out of
    # QUARANTINE (net zero per inspection, one or two rows per bucket; detail: V-RT-03).
    ("RETURN_RECEIPT", "sales_return_line_item_id", """
        SELECT rl.sales_return_line_item_id AS source_id, DATE(rl.received_at) AS business_date,
               rl.quantity_received AS qty
        FROM sales_return_line_items rl WHERE rl.quantity_received > 0""", True),
    ("RETURN_DISPOSITION", "return_inspection_id", """
        SELECT return_inspection_id AS source_id, DATE(inspected_at) AS business_date, 0 AS qty
        FROM return_inspections WHERE quantity_sellable + quantity_defective > 0""", False),
    # Transfers (3.12a): one TRANSFER_OUT per dispatched line (lines are per batch); TRANSFER_IN rows for what
    # arrived (good to SELLABLE, damaged to DEFECTIVE: one or two rows); lost units have none (detail: V-IN-08).
    ("TRANSFER_OUT", "stock_transfer_line_item_id", """
        SELECT l.stock_transfer_line_item_id AS source_id, DATE(t.dispatched_at) AS business_date,
               -l.quantity_shipped AS qty
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        WHERE t.dispatched_at IS NOT NULL AND l.quantity_shipped > 0""", True),
    ("TRANSFER_IN", "stock_transfer_line_item_id", """
        SELECT l.stock_transfer_line_item_id AS source_id, DATE(t.received_at) AS business_date,
               l.quantity_received_good + l.quantity_received_damaged AS qty
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        WHERE t.transfer_status = 'RECEIVED' AND l.quantity_received_good + l.quantity_received_damaged > 0""", False),
    # Defective dispositions (3.12b): SCRAP / RETURN_TO_SUPPLIER write the units off (one row); REPAIR moves them
    # back to SELLABLE (two rows, net zero; detail: V-RT-05).
    ("DEFECTIVE_DISPOSITION", "defective_stock_id", """
        SELECT defective_stock_id AS source_id, DATE(disposed_at) AS business_date,
               IF(disposition = 'REPAIR', 0, -quantity) AS qty
        FROM defective_stock WHERE disposition_status = 'COMPLETED'""", False),
    # Store handover: the invoice line is the source, one row per batch picked.
    ("SALES_ISSUE", "invoice_line_item_id", """
        SELECT il.invoice_line_item_id AS source_id, i.invoice_date AS business_date,
               IF(i.invoice_status = 'CANCELLED', 0, -il.quantity) AS qty
        FROM invoice_line_items il JOIN invoices i ON i.invoice_id = il.invoice_id
        WHERE i.shipment_id IS NULL AND il.line_type = 'PRODUCT'""", False),
]


@check("V-IN-05")
def source_events_have_their_ledger_rows(ctx: ValidationContext) -> Rows:
    """Every source event (inspected GRN line with accepted goods, POSTED adjustment line, dispatched shipment
    line, store-handover invoice line …) has its ledger rows of its type with its quantity (exactly one row,
    reversed pairs aside, except a handover line, which has one per batch), and no ledger row lacks its source
    event. Ledger types whose process isn't generated yet must not occur."""
    found: Rows = []
    for kind, column, events, single in LEDGER_SOURCES:
        rows_rule = "t.rows_ <> 1" if single else "t.rows_ < 1"
        found += [{**r, "transaction_type": kind} for r in ctx.rows(f"""
            SELECT COALESCE(e.source_id, t.source_id) AS source_id, e.qty AS expected_qty, t.qty AS ledger_qty, t.rows_,
                   CASE WHEN e.source_id IS NULL THEN 'ledger row without its source event'
                        WHEN t.source_id IS NULL THEN 'source event without a ledger row'
                        ELSE 'ledger quantity or row count differs' END AS problem
            FROM ({events}) e
            LEFT JOIN (SELECT {column} AS source_id, SUM(quantity) AS qty,
                              SUM(reversal_of_inventory_transaction_id IS NULL) AS rows_
                       FROM inventory_transactions WHERE transaction_type = '{kind}' AND {column} IS NOT NULL
                       GROUP BY {column}) t
                   ON t.source_id = e.source_id
            WHERE t.source_id IS NULL OR t.qty <> e.qty OR {rows_rule}
            UNION ALL
            SELECT t.{column}, NULL, t.quantity, 1, 'ledger row without its source event'
            FROM inventory_transactions t
            WHERE t.transaction_type = '{kind}' AND t.{column} NOT IN (SELECT source_id FROM ({events}) e2)""")]
    generated = {kind for kind, *_ in LEDGER_SOURCES}
    found += [{"transaction_type": t, "problem": "ledger type not generated yet"} for (t,) in ctx.conn.exec_driver_sql(
        "SELECT DISTINCT transaction_type FROM inventory_transactions") if t not in generated]
    return found


@check("V-IN-10")
def ledger_times_follow_their_source(ctx: ValidationContext) -> Rows:
    """``transaction_at`` is not earlier than the source document's business date and lies within the
    simulated window of the run that wrote it."""
    found: Rows = []
    for kind, column, events, _ in LEDGER_SOURCES:
        found += [{**r, "problem": "before its source document's date"} for r in ctx.rows(f"""
            SELECT t.inventory_transaction_id, t.transaction_at, e.business_date FROM inventory_transactions t
            JOIN ({events}) e ON e.source_id = t.{column}
            WHERE t.transaction_type = '{kind}' AND DATE(t.transaction_at) < e.business_date""")]
    found += [{**r, "problem": "outside its run's simulated window"} for r in ctx.rows("""
        SELECT t.inventory_transaction_id, t.transaction_at, r.sim_start_date, r.sim_end_date
        FROM inventory_transactions t JOIN generation_runs r ON r.generation_run_id = t.generation_run_id
        WHERE DATE(t.transaction_at) NOT BETWEEN r.sim_start_date AND r.sim_end_date""")]
    return found


@check("V-IN-03")
def reservations_reconcile(ctx: ValidationContext) -> Rows:
    """★ ``inventory.qty_reserved`` = Σ outstanding reservations of the warehouse × product; a reservation's
    fulfilled quantity = the quantity issued (SALES_ISSUE, net of reversals) for its order line at its warehouse;
    a reservation's status agrees with its quantities; reservations only for lines of open orders' products."""
    found = [{**r, "problem": "qty_reserved ≠ Σ outstanding reservations"} for r in ctx.rows("""
        SELECT k.warehouse_id, k.product_id, COALESCE(i.qty_reserved, 0) AS reserved, COALESCE(r.outstanding, 0) AS outstanding
        FROM (SELECT warehouse_id, product_id FROM inventory WHERE qty_reserved <> 0
              UNION SELECT r.warehouse_id, l.product_id FROM inventory_reservations r
                    JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id) k
        LEFT JOIN inventory i ON i.warehouse_id = k.warehouse_id AND i.product_id = k.product_id
        LEFT JOIN (SELECT r.warehouse_id, l.product_id, SUM(r.outstanding_quantity) AS outstanding
                   FROM inventory_reservations r JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id
                   GROUP BY r.warehouse_id, l.product_id) r ON r.warehouse_id = k.warehouse_id AND r.product_id = k.product_id
        WHERE COALESCE(i.qty_reserved, 0) <> COALESCE(r.outstanding, 0)""")]
    found += [{**r, "problem": "fulfilled ≠ Σ issued for the order line at the warehouse"} for r in ctx.rows("""
        SELECT r.inventory_reservation_id, r.fulfilled_quantity, COALESCE(x.issued, 0) AS issued
        FROM inventory_reservations r
        LEFT JOIN (SELECT t.warehouse_id, COALESCE(s.order_line_item_id, v.order_line_item_id) AS order_line_item_id,
                          -SUM(t.quantity) AS issued
                   FROM inventory_transactions t
                   LEFT JOIN shipment_line_items s ON s.shipment_line_item_id = t.shipment_line_item_id
                   LEFT JOIN invoice_line_items v ON v.invoice_line_item_id = t.invoice_line_item_id
                   WHERE t.transaction_type = 'SALES_ISSUE'
                   GROUP BY t.warehouse_id, COALESCE(s.order_line_item_id, v.order_line_item_id)) x
               ON x.warehouse_id = r.warehouse_id AND x.order_line_item_id = r.order_line_item_id
        WHERE r.fulfilled_quantity <> COALESCE(x.issued, 0)""")]
    found += [{**r, "problem": "status doesn't match the quantities"} for r in ctx.rows("""
        SELECT inventory_reservation_id, reservation_status, reserved_quantity, fulfilled_quantity, released_quantity
        FROM inventory_reservations
        WHERE NOT ((reservation_status = 'ACTIVE' AND fulfilled_quantity = 0 AND released_quantity = 0)
                OR (reservation_status = 'PARTIALLY_FULFILLED' AND fulfilled_quantity > 0 AND outstanding_quantity > 0)
                OR (reservation_status = 'FULFILLED' AND outstanding_quantity = 0 AND fulfilled_quantity > 0)
                OR (reservation_status = 'RELEASED' AND outstanding_quantity = 0 AND released_quantity > 0))""")]
    found += [{**r, "problem": "outstanding reservation on a cancelled order or line"} for r in ctx.rows("""
        SELECT r.inventory_reservation_id, o.order_status, l.line_status FROM inventory_reservations r
        JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id JOIN orders o ON o.order_id = l.order_id
        WHERE r.outstanding_quantity > 0 AND (o.order_status = 'CANCELLED' OR l.line_status = 'CANCELLED')""")]
    return found


ISSUE_TYPES = ("SALES_ISSUE", "TRANSFER_OUT")


@check("V-IN-09")
def issues_at_moving_average_cost(ctx: ValidationContext) -> Rows:
    """Issue unit cost = the warehouse × product moving-average cost at that moment, recomputed by replaying the
    ledger in time order (±0.0001), and the issue value = ROUND(quantity × MAC, 2), or the whole remaining value
    when the issue empties the position (1C §4, D27). Reversal rows are replayed but not judged. (Return
    receipts at the original issue cost join with returns, step 3.11.)"""
    found: Rows = []
    positions: dict[tuple[int, int], tuple[Decimal, Decimal]] = {}
    result = ctx.conn.execute(text("""
        SELECT inventory_transaction_id, warehouse_id, product_id, transaction_type, quantity, unit_cost, value_change,
               reversal_of_inventory_transaction_id FROM inventory_transactions
        ORDER BY warehouse_id, product_id, transaction_at, inventory_transaction_id"""))
    for row in result.mappings():
        key = (row["warehouse_id"], row["product_id"])
        qty, value = positions.get(key, (Decimal(0), Decimal(0)))
        if row["transaction_type"] in ISSUE_TYPES and row["reversal_of_inventory_transaction_id"] is None:
            try:
                _, expected = StockPosition(qty, value).issue(-row["quantity"])
            except (StockError, ValueError) as exc:
                found.append({"inventory_transaction_id": row["inventory_transaction_id"], "problem": str(exc)})
            else:
                if abs(expected.unit_cost - row["unit_cost"]) > Decimal("0.0001") \
                        or expected.value_change != row["value_change"]:
                    found.append({"inventory_transaction_id": row["inventory_transaction_id"],
                                  "unit_cost": row["unit_cost"], "expected_unit_cost": expected.unit_cost,
                                  "value_change": row["value_change"], "expected_value": expected.value_change,
                                  "problem": "issue not at the moving-average cost"})
        positions[key] = (qty + row["quantity"], value + row["value_change"])
    return found


@check("V-IN-06")
def reversals_mirror_their_original(ctx: ValidationContext) -> Rows:
    """Reversal rows (D33) mirror their original: same type, source, warehouse, product, batch and unit cost,
    opposite quantity and value, not earlier; an original is reversed at most once and never itself a reversal."""
    found = [{**r, "problem": "reversal doesn't mirror its original"} for r in ctx.rows("""
        SELECT r.inventory_transaction_id, o.inventory_transaction_id AS original_id FROM inventory_transactions r
        JOIN inventory_transactions o ON o.inventory_transaction_id = r.reversal_of_inventory_transaction_id
        WHERE r.transaction_type <> o.transaction_type OR r.warehouse_id <> o.warehouse_id OR r.product_id <> o.product_id
           OR NOT (r.inventory_batch_id <=> o.inventory_batch_id) OR r.unit_cost <> o.unit_cost
           OR r.quantity <> -o.quantity OR r.value_change <> -o.value_change OR r.transaction_at < o.transaction_at
           OR o.reversal_of_inventory_transaction_id IS NOT NULL
           OR NOT (r.shipment_line_item_id <=> o.shipment_line_item_id)
           OR NOT (r.invoice_line_item_id <=> o.invoice_line_item_id)
           OR NOT (r.goods_receipt_line_item_id <=> o.goods_receipt_line_item_id)
           OR NOT (r.stock_adjustment_line_item_id <=> o.stock_adjustment_line_item_id)
           OR NOT (r.stock_transfer_line_item_id <=> o.stock_transfer_line_item_id)""")]
    found += [{**r, "problem": "original reversed more than once"} for r in ctx.rows("""
        SELECT reversal_of_inventory_transaction_id AS original_id, COUNT(*) AS reversals FROM inventory_transactions
        WHERE reversal_of_inventory_transaction_id IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1""")]
    return found


@check("V-IN-07")
def transfer_tax_treatment(ctx: ValidationContext) -> Rows:
    """Transfers: ``is_inter_gstin`` matches the warehouses' GSTINs; an inter-GSTIN transfer has a tax invoice
    number and IGST at the product's rate on the dispatch date on every line; a same-GSTIN transfer has neither;
    header taxable value and IGST = Σ lines."""
    found = [{**r, "problem": "is_inter_gstin doesn't match the warehouses' GSTINs or the tax invoice number"}
             for r in ctx.rows("""
        SELECT t.stock_transfer_id, t.is_inter_gstin, t.tax_invoice_number FROM stock_transfers t
        JOIN warehouses s ON s.warehouse_id = t.source_warehouse_id
        JOIN warehouses d ON d.warehouse_id = t.destination_warehouse_id
        WHERE t.is_inter_gstin <> (s.company_gst_registration_id <> d.company_gst_registration_id)
           OR (t.dispatched_at IS NOT NULL AND t.is_inter_gstin <> (t.tax_invoice_number IS NOT NULL))""")]
    found += [{**r, "problem": "line GST rate isn't the product's rate on the dispatch date (0 when same GSTIN)"}
              for r in ctx.rows("""
        SELECT l.stock_transfer_line_item_id, l.gst_rate, r.gst_rate AS product_rate, t.is_inter_gstin
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        LEFT JOIN product_tax_rates r ON r.product_id = l.product_id AND r.effective_from <= DATE(t.dispatched_at)
             AND (r.effective_to IS NULL OR r.effective_to >= DATE(t.dispatched_at))
        WHERE t.dispatched_at IS NOT NULL
          AND ((t.is_inter_gstin AND NOT (l.gst_rate <=> r.gst_rate))
               OR (NOT t.is_inter_gstin AND (l.gst_rate <> 0 OR l.igst_amount <> 0)))""")]
    found += [{**r, "problem": "header taxable value or IGST differs from its lines"} for r in ctx.rows("""
        SELECT t.stock_transfer_id, t.taxable_amount, t.igst_amount, SUM(l.transfer_value) AS line_value,
               SUM(l.igst_amount) AS line_igst
        FROM stock_transfers t JOIN stock_transfer_line_items l ON l.stock_transfer_id = t.stock_transfer_id
        GROUP BY t.stock_transfer_id, t.taxable_amount, t.igst_amount, t.is_inter_gstin
        HAVING t.igst_amount <> SUM(l.igst_amount)
            OR t.taxable_amount <> IF(t.is_inter_gstin, SUM(l.transfer_value), 0)""")]
    return found


@check("V-IN-08")
def transfers_reconcile(ctx: ValidationContext) -> Rows:
    """★ Received transfers: good + damaged + lost = shipped per line; the TRANSFER_OUT row is −shipped at the
    source and the TRANSFER_IN rows are +good (SELLABLE) and +damaged (DEFECTIVE) at the destination, with values
    that with the lost value add up to the transfer value; damaged units have their ``defective_stock`` entry;
    GL Inventory in Transit = the value of transfers still in transit (1E V-INV-GL)."""
    found = [{**r, "problem": "received quantities or ledger rows don't add up to what was shipped"}
             for r in ctx.rows("""
        SELECT l.stock_transfer_line_item_id, l.quantity_shipped, l.quantity_received_good,
               l.quantity_received_damaged, l.quantity_lost, o.qty AS out_qty, g.qty AS good_qty, d.qty AS damaged_qty
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        LEFT JOIN (SELECT stock_transfer_line_item_id, SUM(quantity) AS qty, MIN(warehouse_id) AS w FROM inventory_transactions
                   WHERE transaction_type = 'TRANSFER_OUT' GROUP BY 1) o
               ON o.stock_transfer_line_item_id = l.stock_transfer_line_item_id
        LEFT JOIN (SELECT stock_transfer_line_item_id, SUM(quantity) AS qty, MIN(warehouse_id) AS w FROM inventory_transactions
                   WHERE transaction_type = 'TRANSFER_IN' AND stock_status = 'SELLABLE' GROUP BY 1) g
               ON g.stock_transfer_line_item_id = l.stock_transfer_line_item_id
        LEFT JOIN (SELECT stock_transfer_line_item_id, SUM(quantity) AS qty, MIN(warehouse_id) AS w FROM inventory_transactions
                   WHERE transaction_type = 'TRANSFER_IN' AND stock_status = 'DEFECTIVE' GROUP BY 1) d
               ON d.stock_transfer_line_item_id = l.stock_transfer_line_item_id
        WHERE t.dispatched_at IS NOT NULL
          AND (COALESCE(o.qty, 0) <> -l.quantity_shipped OR o.w <> t.source_warehouse_id
               OR (t.transfer_status = 'RECEIVED'
                   AND (l.quantity_received_good + l.quantity_received_damaged + l.quantity_lost <> l.quantity_shipped
                        OR COALESCE(g.qty, 0) <> l.quantity_received_good
                        OR COALESCE(d.qty, 0) <> l.quantity_received_damaged
                        OR g.w <> t.destination_warehouse_id OR d.w <> t.destination_warehouse_id))
               OR (t.transfer_status <> 'RECEIVED' AND (g.qty IS NOT NULL OR d.qty IS NOT NULL)))""")]
    found += [{**r, "problem": "received values + lost value differ from the transfer value"} for r in ctx.rows("""
        SELECT l.stock_transfer_line_item_id, l.transfer_value, COALESCE(SUM(i.value_change), 0) AS received_value
        FROM stock_transfer_line_items l JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
        LEFT JOIN inventory_transactions i ON i.stock_transfer_line_item_id = l.stock_transfer_line_item_id
             AND i.transaction_type = 'TRANSFER_IN'
        WHERE t.transfer_status = 'RECEIVED'
        GROUP BY l.stock_transfer_line_item_id, l.transfer_value, l.quantity_lost
        HAVING (l.quantity_lost = 0 AND received_value <> l.transfer_value)
            OR (l.quantity_lost > 0 AND received_value > l.transfer_value)""")]
    found += [{**r, "problem": "damaged units without exactly one TRANSIT_DAMAGE defective entry of that quantity"}
              for r in ctx.rows("""
        SELECT l.stock_transfer_line_item_id, l.quantity_received_damaged, s.quantity, s.defect_reason
        FROM stock_transfer_line_items l
        LEFT JOIN defective_stock s ON s.stock_transfer_line_item_id = l.stock_transfer_line_item_id
        WHERE (l.quantity_received_damaged > 0 AND (s.defective_stock_id IS NULL OR s.quantity <> l.quantity_received_damaged
                                                   OR s.defect_reason <> 'TRANSIT_DAMAGE'))
           OR (l.quantity_received_damaged = 0 AND s.defective_stock_id IS NOT NULL)""")]
    gl = ctx.scalar("""
        SELECT COALESCE(SUM(l.debit_amount - l.credit_amount), 0) FROM journal_entry_lines l
        JOIN chart_of_accounts a ON a.account_id = l.account_id WHERE a.system_role = 'INVENTORY_IN_TRANSIT'""")
    open_value = ctx.scalar("""
        SELECT COALESCE(SUM(l.transfer_value), 0) FROM stock_transfer_line_items l
        JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id WHERE t.transfer_status = 'IN_TRANSIT'""")
    if Decimal(gl) != Decimal(open_value):
        found.append({"gl_inventory_in_transit": gl, "open_transfer_value": open_value,
                      "problem": "GL Inventory in Transit differs from the value of transfers in transit"})
    return found
