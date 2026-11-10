"""Phase 3 step 3.7a: sourcing on generated data (RFQ → quotations → award → PO → approval).

Demand only arrives in step 3.8, so the fixture forces replenishment: after three weeks, the reorder level of
half the DC stock is raised to the quantity on hand
(a quarter with a large maximum, so their orders exceed the direct-reorder limit and go to RFQ). The window includes the 22 Sep 2025 GST rate change.
Every procurement rule must pass, and each mutation (rolled back) must make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.validation import framework
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
RULES = sorted(r for r in framework.registered_checks() if r.startswith("V-PR") or r == "V-MD-06")
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-11-30"}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zp" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty"], base_seed=7)
    try:
        config = load_generation_config(preset="small", today=date(2026, 11, 10), overrides=WINDOW)
        generate_history(settings, code, config, days=21)
        engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
        with engine.connect() as conn:
            conn.execute(text("""
                UPDATE inventory i JOIN warehouses w ON w.warehouse_id = i.warehouse_id
                SET i.reorder_level = i.qty_sellable,
                    i.max_stock_level = i.qty_sellable + i.reorder_quantity * IF(i.product_id % 4 = 0, 40, 1)
                WHERE w.warehouse_type = 'DISTRIBUTION_CENTRE' AND i.product_id % 2 = 0 AND i.qty_sellable > 0"""))
            conn.commit()
        result = generate_history(settings, code, config)
        assert result.report.passed, [r.rule_id for r in result.report.results if r.status not in ("PASSED", "PENDING")]
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


def test_sourcing_produces_the_whole_document_chain(generated):
    _, conn = generated
    report_rules = dict(_rows(conn, "SELECT rfq_type, COUNT(*) FROM rfq_headers GROUP BY 1"))
    assert report_rules.get("REPLENISHMENT", 0) > 0 and report_rules.get("NEW_PRODUCT", 0) > 0
    sources = dict(_rows(conn, "SELECT po_source, COUNT(*) FROM purchase_order_headers GROUP BY 1"))
    assert sources.get("RFQ_AWARD", 0) > 0 and sources.get("DIRECT_REORDER", 0) > 0
    responses = dict(_rows(conn, "SELECT response_status, COUNT(*) FROM rfq_suppliers GROUP BY 1"))
    assert {"QUOTED", "DECLINED", "NO_RESPONSE"} <= set(responses)
    assert responses["QUOTED"] > responses["DECLINED"]
    quotations = {s for (s,) in _rows(conn, "SELECT DISTINCT quotation_status FROM quotation_headers")}
    assert {"ACCEPTED", "REJECTED"} <= quotations
    po_statuses = {s for (s,) in _rows(conn, "SELECT DISTINCT po_status FROM purchase_order_headers")}
    assert "SENT" in po_statuses and po_statuses <= {"PENDING_APPROVAL", "SENT", "CANCELLED"}
    # Purchasing works on working days only.
    assert _rows(conn, "SELECT COUNT(*) FROM purchase_order_headers WHERE DAYOFWEEK(po_date) = 1")[0][0] == 0


def test_stock_is_never_ordered_twice(generated):
    """Stock on order and being sourced counts in the reorder position: one open supply per DC × product."""
    _, conn = generated
    duplicates = _rows(conn, """
        SELECT h.delivery_warehouse_id, l.product_id, COUNT(*) FROM purchase_order_line_items l
        JOIN purchase_order_headers h USING (purchase_order_id)
        WHERE h.po_status NOT IN ('CANCELLED') GROUP BY 1, 2 HAVING COUNT(*) > 1""")
    assert duplicates == []


def test_award_after_rate_change_uses_the_new_rate(generated):
    """GST follows the supply date: POs on or after 22 Sep 2025 never carry the retired 28% slab."""
    _, conn = generated
    assert _rows(conn, "SELECT COUNT(*) FROM purchase_order_line_items l JOIN purchase_order_headers h "
                       "USING (purchase_order_id) WHERE h.po_date >= '2025-09-22' AND l.gst_rate = 28")[0][0] == 0


def test_generated_data_passes_procurement_rules(generated):
    code, conn = generated
    report = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=RULES)
    assert {r.rule_id: r.status for r in report.results if r.status != "PASSED"} == {}


_ONE_PO = "(SELECT x FROM (SELECT MIN(purchase_order_id) x FROM purchase_order_headers WHERE po_source = '{}') t)"
_AWARD_PO = _ONE_PO.format("RFQ_AWARD")

MUTATIONS = {
    "V-MD-06": [f"UPDATE purchase_order_headers SET approved_by_employee_id = (SELECT e FROM (SELECT MIN(employee_id) e "
                f"FROM employees WHERE approval_limit = 0) t) WHERE purchase_order_id = {_AWARD_PO}"],
    "V-PR-01": [f"UPDATE purchase_order_line_items SET unit_price = unit_price + 1, "
                f"taxable_amount = ROUND(ordered_quantity * (unit_price) * (1 - discount_percent / 100), 2), "
                f"cgst_amount = IF(igst_amount = 0, ROUND(taxable_amount * gst_rate / 200, 2), 0), "
                f"sgst_amount = cgst_amount, igst_amount = IF(igst_amount = 0, 0, ROUND(taxable_amount * gst_rate / 100, 2)), "
                f"line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount "
                f"WHERE purchase_order_id = {_AWARD_PO} AND line_number = 1"],
    "V-PR-02": [f"DELETE FROM purchase_order_status_history WHERE purchase_order_id = {_AWARD_PO} AND to_status = 'APPROVED'"],
    "V-PR-03": ["UPDATE rfq_headers SET business_unit_id = (SELECT b FROM (SELECT MAX(business_unit_id) b FROM business_units) t) "
                "WHERE rfq_id = (SELECT r FROM (SELECT MIN(rfq_id) r FROM rfq_line_items l JOIN products p USING (product_id) "
                "WHERE p.business_unit_id <> (SELECT MAX(business_unit_id) FROM business_units)) t)"],
    "V-PR-04": ["UPDATE rfq_suppliers SET response_status = 'QUOTED', responded_date = invited_date "
                "WHERE rfq_supplier_id = (SELECT r FROM (SELECT MIN(rfq_supplier_id) r FROM rfq_suppliers "
                "WHERE response_status = 'NO_RESPONSE') t)"],
    "V-PR-05": ["UPDATE quotation_headers SET quotation_status = 'ACCEPTED', rejection_reason = NULL "
                "WHERE quotation_id = (SELECT q FROM (SELECT MIN(quotation_id) q FROM quotation_headers "
                "WHERE quotation_status = 'REJECTED') t)"],
    "V-PR-06": ["UPDATE quotation_headers q JOIN rfq_suppliers rs USING (rfq_supplier_id) JOIN rfq_headers h USING (rfq_id) "
                "SET q.quotation_date = DATE_SUB(h.rfq_date, INTERVAL 1 DAY), q.valid_until_date = h.rfq_date "
                "WHERE q.quotation_id = (SELECT x FROM (SELECT MIN(quotation_id) x FROM quotation_headers) t)"],
    "V-PR-07": [f"UPDATE purchase_order_headers SET supplier_id = (SELECT s FROM (SELECT MIN(supplier_id) s FROM suppliers "
                f"WHERE supplier_id <> (SELECT supplier_id FROM purchase_order_headers WHERE purchase_order_id = {_AWARD_PO})) t) "
                f"WHERE purchase_order_id = {_AWARD_PO}"],
}


def test_every_rule_here_has_a_mutation():
    assert set(MUTATIONS) == set(RULES)


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, rule_id):
    code, conn = generated
    try:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))     # V-PR-07 swaps a supplier the composite keys protect
        for statement in MUTATIONS[rule_id]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
