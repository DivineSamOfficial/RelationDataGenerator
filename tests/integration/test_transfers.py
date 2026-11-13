"""Phase 3 step 3.12a: stock transfers — weekly store replenishment from the supplying DC (same GSTIN, no tax),
monthly DC rebalancing (inter-GSTIN: tax invoice, IGST, two tax rows per line, D29/D55), P16 at dispatch, P17 at
receipt, damage and loss in transit, on generated data.

Window: January–April 2025, Small preset, with transit damage and loss raised so that both occur. Every transfer
rule must pass, and each mutation (rolled back) must make its rule fail.
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
RULES = ("V-IN-07", "V-IN-08")
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-04-30",
          "inventory.transit_damage_share": 0.25, "inventory.transit_loss_share": 0.15}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zx" + uuid.uuid4().hex[:6]
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


def test_store_replenishment(generated):
    _, conn = generated
    rows = _rows(conn, """
        SELECT t.stock_transfer_id, t.source_warehouse_id, d.replenishment_warehouse_id, t.is_inter_gstin,
               t.tax_invoice_number, t.shipping_carrier_id, t.taxable_amount, t.igst_amount, t.requested_date,
               t.expected_arrival_date, d.warehouse_id
        FROM stock_transfers t JOIN warehouses d ON d.warehouse_id = t.destination_warehouse_id
        WHERE t.transfer_type = 'STORE_REPLENISHMENT'""")
    assert rows
    # From the store's own DC, same GSTIN: a delivery challan without tax, own vehicle, arriving the next day.
    assert all(r[1] == r[2] and not r[3] and r[4] is None and r[5] is None and r[6] == r[7] == 0 for r in rows)
    assert all(1 <= (r[9] - r[8]).days <= 3 for r in rows)
    # Weekly, each store on its own weekday (warehouse id mod 6).
    assert all(r[8].weekday() == r[10] % 6 for r in rows)
    ids = ", ".join(str(r[0]) for r in rows)
    assert _one(conn, f"""SELECT COUNT(*) FROM tax_transactions x JOIN stock_transfer_line_items l
                          USING (stock_transfer_line_item_id) WHERE l.stock_transfer_id IN ({ids})""") == 0


def test_dc_rebalancing(generated):
    _, conn = generated
    rows = _rows(conn, """
        SELECT t.stock_transfer_id, t.is_inter_gstin, t.tax_invoice_number, c.carrier_type, t.requested_date,
               t.taxable_amount, t.igst_amount
        FROM stock_transfers t LEFT JOIN shipping_carriers c USING (shipping_carrier_id)
        WHERE t.transfer_type = 'REBALANCING'""")
    assert rows
    # DC to DC is always another state's GSTIN: a tax invoice (T series), IGST, a road carrier, monthly.
    assert all(r[1] and r[2] and r[2][7] == "T" and r[3] in ("SURFACE_LTL", "FULL_TRUCKLOAD") for r in rows)
    assert all(r[4].day >= 5 and r[4].day <= 9 for r in rows)
    assert sum(r[6] for r in rows) > 0
    # Two tax rows per line (OUTPUT at the source, INPUT at the destination) with the line's IGST (D29).
    assert _one(conn, """
        SELECT COUNT(*) FROM stock_transfer_line_items l JOIN stock_transfers t USING (stock_transfer_id)
        WHERE t.is_inter_gstin AND (SELECT COUNT(*) FROM tax_transactions x
                                    WHERE x.stock_transfer_line_item_id = l.stock_transfer_line_item_id
                                      AND x.igst_amount = l.igst_amount) <> 2""") == 0
    # The source DC had more than its maximum left after the transfer was decided; the destination was short.
    assert _one(conn, "SELECT COUNT(*) FROM stock_transfers WHERE source_warehouse_id = destination_warehouse_id") == 0


def test_transfer_journals(generated):
    _, conn = generated
    # P16 per dispatched transfer: Cr Inventory (source) = Dr In Transit = Σ transfer values; IGST both sides.
    assert _one(conn, """
        SELECT COUNT(*) FROM stock_transfers t
        LEFT JOIN journal_entries j ON j.stock_transfer_id = t.stock_transfer_id AND j.journal_source = 'TRANSFER_DISPATCH'
        LEFT JOIN (SELECT stock_transfer_id, SUM(transfer_value) AS v, SUM(igst_amount) AS g
                   FROM stock_transfer_line_items GROUP BY 1) l ON l.stock_transfer_id = t.stock_transfer_id
        WHERE j.journal_entry_id IS NULL OR j.total_debit <> l.v + l.g""") == 0
    # P17 per received transfer, clearing In Transit by the full transfer value.
    assert _one(conn, """
        SELECT COUNT(*) FROM stock_transfers t
        LEFT JOIN journal_entries j ON j.stock_transfer_id = t.stock_transfer_id AND j.journal_source = 'TRANSFER_RECEIPT'
        LEFT JOIN (SELECT stock_transfer_id, SUM(transfer_value) AS v FROM stock_transfer_line_items GROUP BY 1) l
               ON l.stock_transfer_id = t.stock_transfer_id
        WHERE t.transfer_status = 'RECEIVED' AND (j.journal_entry_id IS NULL OR j.total_debit <> l.v)""") == 0
    in_transit = _one(conn, """SELECT COALESCE(SUM(l.debit_amount - l.credit_amount), 0) FROM journal_entry_lines l
                               JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'INVENTORY_IN_TRANSIT'""")
    open_value = _one(conn, """SELECT COALESCE(SUM(l.transfer_value), 0) FROM stock_transfer_line_items l
                               JOIN stock_transfers t USING (stock_transfer_id) WHERE t.transfer_status = 'IN_TRANSIT'""")
    assert in_transit == open_value


def test_damage_and_loss_in_transit(generated):
    _, conn = generated
    damaged, lost = _rows(conn, """SELECT SUM(quantity_received_damaged > 0), SUM(quantity_lost > 0)
                                   FROM stock_transfer_line_items""")[0]
    assert damaged > 0 and lost > 0
    # Damaged units enter DEFECTIVE at the destination with a TRANSIT_DAMAGE register entry (D45).
    assert _one(conn, """SELECT COUNT(*) FROM stock_transfer_line_items l
                         JOIN defective_stock s USING (stock_transfer_line_item_id)
                         JOIN stock_transfers t ON t.stock_transfer_id = l.stock_transfer_id
                         WHERE s.defect_reason <> 'TRANSIT_DAMAGE' OR s.warehouse_id <> t.destination_warehouse_id
                            OR s.quantity <> l.quantity_received_damaged""") == 0
    # Lost units are written off in P17 (Dr Inventory Write-offs at the destination's cost centre).
    write_offs = _one(conn, """SELECT COUNT(*) FROM journal_entries j JOIN journal_entry_lines l USING (journal_entry_id)
                               JOIN chart_of_accounts a USING (account_id)
                               WHERE j.journal_source = 'TRANSFER_RECEIPT' AND a.system_role = 'INVENTORY_WRITE_OFF'""")
    assert write_offs == _one(conn, """SELECT COUNT(DISTINCT stock_transfer_id) FROM stock_transfer_line_items
                                       WHERE quantity_lost > 0""")


def test_transfer_timing(generated):
    _, conn = generated
    rows = _rows(conn, "SELECT dispatched_at, received_at, created_at FROM stock_transfers WHERE transfer_status = 'RECEIVED'")
    assert all(d.time() >= time(19) and r.time() < time(9, 1) and c < d < r for d, r, c in rows)
    # A receipt is the first movement of its product at the destination that day; a dispatch the last at the source.
    assert _one(conn, """
        SELECT COUNT(*) FROM inventory_transactions i JOIN inventory_transactions o
             ON o.warehouse_id = i.warehouse_id AND o.product_id = i.product_id
            AND DATE(o.transaction_at) = DATE(i.transaction_at) AND o.transaction_at < i.transaction_at
            AND o.transaction_type <> 'TRANSFER_IN' AND o.reversal_of_inventory_transaction_id IS NULL
        WHERE i.transaction_type = 'TRANSFER_IN'""") == 0      # (a D40 reversal is a receipt: order-free)
    assert _one(conn, """
        SELECT COUNT(*) FROM inventory_transactions o JOIN inventory_transactions x
             ON x.warehouse_id = o.warehouse_id AND x.product_id = o.product_id
            AND DATE(x.transaction_at) = DATE(o.transaction_at) AND x.transaction_at > o.transaction_at
            AND x.transaction_type <> 'TRANSFER_OUT'
        WHERE o.transaction_type = 'TRANSFER_OUT'""") == 0


def test_transfers_keep_shelf_life(generated):
    _, conn = generated
    # Batches sent to another warehouse arrive with at least min_shelf_days (30) of shelf life left.
    assert _one(conn, """SELECT COUNT(*) FROM stock_transfer_line_items l JOIN stock_transfers t USING (stock_transfer_id)
                         JOIN inventory_batches b ON b.inventory_batch_id = l.inventory_batch_id
                         WHERE b.expiry_date < t.expected_arrival_date + INTERVAL 30 DAY""") == 0


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_STORE_TRANSFER = _FIRST.format(key="stock_transfer_id", table="stock_transfers", where="NOT is_inter_gstin")
_TAXED_LINE = _FIRST.format(key="stock_transfer_line_item_id", table="stock_transfer_line_items", where="igst_amount > 0")
_RECEIVED_LINE = _FIRST.format(key="stock_transfer_line_item_id", table="stock_transfer_line_items",
                               where="quantity_received_good > 0 AND gst_rate = 0")
_TRANSFER_IN = _FIRST.format(key="inventory_transaction_id", table="inventory_transactions",
                             where="transaction_type = 'TRANSFER_IN' AND stock_transfer_line_item_id IN "
                                   "(SELECT stock_transfer_line_item_id FROM stock_transfer_line_items "
                                   "WHERE quantity_lost = 0)")
_TRANSFER_TAX = _FIRST.format(key="tax_transaction_id", table="tax_transactions", where="document_type = 'TRANSFER_IN'")
_DAMAGED_LINE = _FIRST.format(key="stock_transfer_line_item_id", table="stock_transfer_line_items",
                              where="quantity_received_damaged > 0")

MUTATIONS = {
    "V-IN-07": [f"UPDATE stock_transfers SET is_inter_gstin = TRUE, tax_invoice_number = 'ZZ2425-T999999' "
                f"WHERE stock_transfer_id = {_STORE_TRANSFER}"],
    "V-IN-07/rate": [f"UPDATE stock_transfer_line_items SET gst_rate = gst_rate + 1, "
                     f"igst_amount = ROUND(transfer_value * gst_rate / 100, 2) "
                     f"WHERE stock_transfer_line_item_id = {_TAXED_LINE}"],
    "V-IN-08": [f"UPDATE stock_transfer_line_items SET quantity_lost = quantity_lost + 1, "
                f"quantity_shipped = quantity_shipped + 1, quantity_requested = quantity_requested + 1, "
                f"transfer_value = ROUND(quantity_shipped * unit_cost, 2) "
                f"WHERE stock_transfer_line_item_id = {_RECEIVED_LINE}"],
    "V-IN-08/value": [f"UPDATE inventory_transactions SET value_change = value_change + 1 "
                      f"WHERE inventory_transaction_id = {_TRANSFER_IN}"],
    "V-IN-08/defect": [f"DELETE FROM defective_stock WHERE stock_transfer_line_item_id = {_DAMAGED_LINE}"],
    "V-IN-05/transfer": [f"UPDATE inventory_transactions SET quantity = quantity + 1 "
                         f"WHERE inventory_transaction_id = {_TRANSFER_IN}"],
    "V-TX-01/transfer": [f"UPDATE tax_transactions SET igst_amount = igst_amount + 1 WHERE tax_transaction_id = {_TRANSFER_TAX}"],
}


def test_every_rule_has_a_mutation():
    assert {key.split("/")[0] for key in MUTATIONS} >= set(RULES)


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
