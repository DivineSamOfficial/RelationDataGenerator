"""Inventory rules (1C §8, V-IN-*). Each check returns the violating rows.

Implemented: ★ V-IN-01, V-IN-02, V-IN-04 (opening stock, 3.6b); V-IN-05, V-IN-10 (receipts, 3.7b). The others need the documents
of later steps (reservations, transfers, issue costs, source dates).
"""

from __future__ import annotations

from typing import Any

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


# V-IN-05 / V-IN-10: (ledger type, source column, source event SQL: id, business date, quantity it must move).
# Each process step adds its source events here.
LEDGER_SOURCES = {
    "PURCHASE_RECEIPT": ("goods_receipt_line_item_id", """
        SELECT gl.goods_receipt_line_item_id AS source_id, g.receipt_date AS business_date, gi.quantity_accepted AS qty
        FROM goods_receipt_line_items gl JOIN goods_receipts g ON g.goods_receipt_id = gl.goods_receipt_id
        JOIN goods_inspections gi ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
        WHERE g.grn_status = 'INSPECTED' AND gi.quantity_accepted > 0"""),
    "ADJUSTMENT": ("stock_adjustment_line_item_id", """
        SELECT l.stock_adjustment_line_item_id AS source_id, a.adjustment_date AS business_date, l.quantity_change AS qty
        FROM stock_adjustment_line_items l JOIN stock_adjustments a ON a.stock_adjustment_id = l.stock_adjustment_id
        WHERE a.adjustment_status = 'POSTED' AND l.quantity_change <> 0"""),
}


@check("V-IN-05")
def source_events_have_their_ledger_rows(ctx: ValidationContext) -> Rows:
    """Every source event (inspected GRN line with accepted goods, POSTED adjustment line …) has exactly one
    ledger row of its type with its quantity (reversed pairs aside), and no ledger row lacks its source event.
    Ledger types whose process isn't generated yet must not occur."""
    found: Rows = []
    for kind, (column, events) in LEDGER_SOURCES.items():
        found += [{**r, "transaction_type": kind} for r in ctx.rows(f"""
            SELECT COALESCE(e.source_id, t.source_id) AS source_id, e.qty AS expected_qty, t.qty AS ledger_qty, t.rows_,
                   CASE WHEN e.source_id IS NULL THEN 'ledger row without its source event'
                        WHEN t.source_id IS NULL THEN 'source event without a ledger row'
                        ELSE 'ledger quantity or row count differs' END AS problem
            FROM ({events}) e
            LEFT JOIN (SELECT {column} AS source_id, SUM(quantity) AS qty,
                              SUM(reversal_of_inventory_transaction_id IS NULL) AS rows_
                       FROM inventory_transactions WHERE transaction_type = '{kind}' GROUP BY {column}) t
                   ON t.source_id = e.source_id
            WHERE t.source_id IS NULL OR t.qty <> e.qty OR t.rows_ <> 1
            UNION ALL
            SELECT t.{column}, NULL, t.quantity, 1, 'ledger row without its source event'
            FROM inventory_transactions t
            WHERE t.transaction_type = '{kind}' AND t.{column} NOT IN (SELECT source_id FROM ({events}) e2)""")]
    found += [{"transaction_type": t, "problem": "ledger type not generated yet"} for (t,) in ctx.conn.exec_driver_sql(
        "SELECT DISTINCT transaction_type FROM inventory_transactions") if t not in LEDGER_SOURCES]
    return found


@check("V-IN-10")
def ledger_times_follow_their_source(ctx: ValidationContext) -> Rows:
    """``transaction_at`` is not earlier than the source document's business date and lies within the
    simulated window of the run that wrote it."""
    found: Rows = []
    for kind, (column, events) in LEDGER_SOURCES.items():
        found += [{**r, "problem": "before its source document's date"} for r in ctx.rows(f"""
            SELECT t.inventory_transaction_id, t.transaction_at, e.business_date FROM inventory_transactions t
            JOIN ({events}) e ON e.source_id = t.{column}
            WHERE t.transaction_type = '{kind}' AND DATE(t.transaction_at) < e.business_date""")]
    found += [{**r, "problem": "outside its run's simulated window"} for r in ctx.rows("""
        SELECT t.inventory_transaction_id, t.transaction_at, r.sim_start_date, r.sim_end_date
        FROM inventory_transactions t JOIN generation_runs r ON r.generation_run_id = t.generation_run_id
        WHERE DATE(t.transaction_at) NOT BETWEEN r.sim_start_date AND r.sim_end_date""")]
    return found
