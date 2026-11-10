"""Inventory rules (1C §8, V-IN-*). Each check returns the violating rows.

Implemented with the opening stock (step 3.6b): ★ V-IN-01, V-IN-02, V-IN-04. The others need the documents
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
