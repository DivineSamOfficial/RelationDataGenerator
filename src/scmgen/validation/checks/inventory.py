"""Inventory rules (1C §8, V-IN-*). Each check returns the violating rows.

Implemented: ★ V-IN-01, V-IN-02, V-IN-04 (opening stock, 3.6b); V-IN-05, V-IN-10 (receipts, 3.7b); ★ V-IN-03
(reservations, 3.8b); V-IN-09 and the sales issues of V-IN-05 (dispatch and handover, 3.9a). The others need
the documents of later steps (transfers).
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
