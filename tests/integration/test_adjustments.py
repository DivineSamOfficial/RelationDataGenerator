"""Phase 3 step 3.12b: expiry write-offs, monthly cycle counts (ABC), handling damage and defective dispositions
(P15, P18), on generated data.

Window: January–March 2025, Small preset (March counts every ABC class). Every rule involved must pass, and each
mutation (rolled back) must make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-03-31"}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zj" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty", "fmcg"],
                     base_seed=7)
    try:
        config = load_generation_config(preset="small", today=date(2026, 11, 10), overrides=WINDOW)
        result = generate_history(settings, code, config)
        assert result.report.passed, [r.rule_id for r in result.report.results if r.status not in ("PASSED", "PENDING")]
        engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
        with engine.connect() as conn:
            yield code, conn
        engine.dispose()
    finally:
        admin = create_mysql_engine(settings, role="app")
        with admin.connect() as conn:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
            conn.commit()
        admin.dispose()
        folder = settings.data_dir / "reports" / code
        for path in folder.glob("*.json"):
            path.unlink()
        if folder.exists():
            folder.rmdir()


def _rows(conn, sql):
    return conn.execute(text(sql)).all()


def _one(conn, sql):
    return conn.execute(text(sql)).scalar()


def test_adjustment_journals(generated):
    _, conn = generated
    # P15: one journal per adjustment with value; debit = losses + gains of its lines; none at zero value.
    assert _one(conn, """
        SELECT COUNT(*) FROM stock_adjustments a
        JOIN (SELECT stock_adjustment_id, SUM(ABS(value_change)) AS gross FROM stock_adjustment_line_items GROUP BY 1) l
             USING (stock_adjustment_id)
        LEFT JOIN journal_entries j ON j.stock_adjustment_id = a.stock_adjustment_id
        WHERE a.adjustment_type <> 'OPENING_STOCK'
          AND ((l.gross > 0 AND (j.journal_entry_id IS NULL OR j.total_debit <> l.gross))
               OR (l.gross = 0 AND j.journal_entry_id IS NOT NULL))""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM stock_adjustments WHERE adjustment_type <> 'OPENING_STOCK'
                         AND (adjustment_status <> 'POSTED' OR approved_by_employee_id IS NULL)""") == 0


def test_expiry_sweep(generated):
    _, conn = generated
    rows = _rows(conn, """SELECT l.reason_code, l.stock_status, l.quantity_change, b.expiry_date, a.adjustment_date,
                                 a.posted_at FROM stock_adjustments a JOIN stock_adjustment_line_items l USING (stock_adjustment_id)
                          JOIN inventory_batches b ON b.inventory_batch_id = l.inventory_batch_id
                          WHERE a.adjustment_type = 'EXPIRY_WRITE_OFF'""")
    assert rows
    assert all(r[0] == "EXPIRED" and r[1] == "SELLABLE" and r[2] < 0 and r[3] <= r[4] and r[5].time() < time(6, 30)
               for r in rows)
    # Expired stock doesn't linger: what is left of a batch expired over a week ago is still reserved at most.
    assert _one(conn, """
        SELECT COUNT(*) FROM (SELECT t.warehouse_id, t.product_id, SUM(t.quantity) AS q FROM inventory_transactions t
                              JOIN inventory_batches b USING (inventory_batch_id)
                              WHERE t.stock_status = 'SELLABLE' AND b.expiry_date < '2025-03-24' GROUP BY 1, 2) x
        JOIN inventory i ON i.warehouse_id = x.warehouse_id AND i.product_id = x.product_id
        WHERE x.q > i.qty_reserved""") == 0


def test_cycle_counts(generated):
    _, conn = generated
    lines = _rows(conn, """SELECT l.reason_code, l.system_quantity, l.counted_quantity, l.quantity_change, a.adjustment_date,
                                  a.posted_at FROM stock_adjustments a JOIN stock_adjustment_line_items l USING (stock_adjustment_id)
                           WHERE a.adjustment_type = 'CYCLE_COUNT'""")
    assert {r[0] for r in lines} == {"FOUND", "THEFT_OR_LOSS"}
    assert all(r[2] == r[1] + r[3] and (r[3] > 0) == (r[0] == "FOUND") for r in lines)
    assert all(r[4].day >= 25 and r[5].time() < time(7) for r in lines)
    # Monthly A items, every class at the end of March: nearly every stocked row has been counted.
    counted, rows = _rows(conn, "SELECT SUM(last_counted_at >= '2025-03-25'), SUM(qty_sellable > 0) FROM inventory")[0]
    assert counted >= rows * Decimal("0.9")
    # Shrinkage stays a small share of the stock value (design: ~0.2-0.5% of counted value per count).
    loss = -_one(conn, """SELECT SUM(value_change) FROM stock_adjustment_line_items WHERE reason_code = 'THEFT_OR_LOSS'""")
    stock = _one(conn, "SELECT SUM(stock_value) FROM inventory")
    assert 0 < loss < stock * Decimal("0.01")


def test_handling_damage_and_dispositions(generated):
    _, conn = generated
    # Damage: a SELLABLE → DEFECTIVE reclass without value change, with its HANDLING_DAMAGE register entry.
    assert _one(conn, """SELECT COUNT(*) FROM stock_adjustments a JOIN stock_adjustment_line_items l USING (stock_adjustment_id)
                         WHERE a.adjustment_type = 'DAMAGE' AND l.value_change <> 0""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM stock_adjustments a
                         WHERE a.adjustment_type = 'DAMAGE' AND NOT EXISTS (
                             SELECT 1 FROM stock_adjustment_line_items l JOIN defective_stock d USING (stock_adjustment_line_item_id)
                             WHERE l.stock_adjustment_id = a.stock_adjustment_id AND d.defect_reason = 'HANDLING_DAMAGE')""") == 0
    # Dispositions: weekly on Wednesday, after the minimum wait; every outcome occurs; repairs carry a memo cost.
    done = _rows(conn, """SELECT disposition, disposed_at, detected_at, repair_cost_amount, supplier_id
                          FROM defective_stock WHERE disposition_status = 'COMPLETED'""")
    assert {r[0] for r in done} == {"SCRAP", "RETURN_TO_SUPPLIER", "REPAIR"}
    assert all(r[1].weekday() == 2 and (r[1].date() - r[2].date()).days >= 4 for r in done)
    assert all((r[3] is not None) == (r[0] == "REPAIR") and (r[4] is not None) == (r[0] == "RETURN_TO_SUPPLIER")
               for r in done)


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_SCRAPPED = _FIRST.format(key="inventory_transaction_id", table="inventory_transactions",
                          where="transaction_type = 'DEFECTIVE_DISPOSITION' AND defective_stock_id IN "
                                "(SELECT defective_stock_id FROM defective_stock WHERE disposition = 'SCRAP')")
_DAMAGE_DEFECT = _FIRST.format(key="defective_stock_id", table="defective_stock", where="stock_adjustment_line_item_id IS NOT NULL")
_SCRAP = _FIRST.format(key="defective_stock_id", table="defective_stock", where="disposition = 'SCRAP'")
_RTS = _FIRST.format(key="defective_stock_id", table="defective_stock", where="disposition = 'RETURN_TO_SUPPLIER'")
_COUNT_ROW = _FIRST.format(key="inventory_transaction_id", table="inventory_transactions",
                           where="transaction_type = 'ADJUSTMENT' AND stock_adjustment_line_item_id IN "
                                 "(SELECT stock_adjustment_line_item_id FROM stock_adjustment_line_items "
                                 "WHERE reason_code = 'THEFT_OR_LOSS')")

MUTATIONS = {
    "V-RT-05/ledger": [f"UPDATE inventory_transactions SET quantity = quantity - 1 WHERE inventory_transaction_id = {_SCRAPPED}"],
    "V-RT-05/damage": [f"UPDATE defective_stock SET quantity = quantity + 1 WHERE defective_stock_id = {_DAMAGE_DEFECT}"],
    "V-RT-05/journal": [f"DELETE FROM journal_entry_lines WHERE journal_entry_id = (SELECT journal_entry_id FROM journal_entries "
                        f"WHERE defective_stock_id = {_SCRAP})",
                        f"DELETE FROM journal_entries WHERE defective_stock_id = {_SCRAP}"],
    "V-RT-05/supplier": [f"UPDATE defective_stock SET supplier_id = (SELECT MAX(supplier_id) FROM suppliers "
                         f"WHERE supplier_id NOT IN (SELECT supplier_id FROM supplier_products WHERE product_id = "
                         f"(SELECT product_id FROM (SELECT product_id FROM defective_stock WHERE defective_stock_id = {_RTS}) y))) "
                         f"WHERE defective_stock_id = {_RTS}"],
    "V-IN-05/adjustment": [f"UPDATE inventory_transactions SET quantity = quantity - 1 WHERE inventory_transaction_id = {_COUNT_ROW}"],
    "V-IN-05/disposition": [f"DELETE FROM inventory_transactions WHERE inventory_transaction_id = {_SCRAPPED}"],
}


@pytest.mark.parametrize("key", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, key):
    code, conn = generated
    rule_id = key.split("/")[0]
    try:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for statement in MUTATIONS[key]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
