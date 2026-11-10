"""Phase 3 step 3.7: procure-to-pay on generated data (RFQ → quotations → award → PO → approval →
deliveries → GRN + dock inspection → stock and P03 → supplier invoice, P04, AP → payment run, P05).

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
RULES = sorted(r for r in framework.registered_checks() if r.startswith(("V-PR", "V-AP", "V-TX")) or r == "V-MD-06")
INVENTORY_RULES = ("V-IN-01", "V-IN-02", "V-IN-04", "V-IN-05", "V-IN-10", "V-AC-05", "V-AC-11")
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
    assert {"CLOSED", "CANCELLED"} <= po_statuses and po_statuses & {"RECEIVED", "PARTIALLY_RECEIVED", "SENT"}
    # Purchasing works on working days only.
    assert _rows(conn, "SELECT COUNT(*) FROM purchase_order_headers WHERE DAYOFWEEK(po_date) = 1")[0][0] == 0


def test_stock_is_never_ordered_twice(generated):
    """Stock on order and being sourced counts in the reorder position, so one stock check never orders a DC ×
    product twice. (A second open PO on a later day is normal once sales reservations have used up the stock
    the first one covers, step 3.8b.)"""
    _, conn = generated
    duplicates = _rows(conn, """
        SELECT h.delivery_warehouse_id, l.product_id, h.po_date, COUNT(*) FROM purchase_order_line_items l
        JOIN purchase_order_headers h USING (purchase_order_id)
        WHERE h.po_status <> 'CANCELLED' GROUP BY 1, 2, 3 HAVING COUNT(*) > 1""")
    assert duplicates == []


def test_award_after_rate_change_uses_the_new_rate(generated):
    """GST follows the supply date: POs on or after 22 Sep 2025 never carry the retired 28% slab."""
    _, conn = generated
    assert _rows(conn, "SELECT COUNT(*) FROM purchase_order_line_items l JOIN purchase_order_headers h "
                       "USING (purchase_order_id) WHERE h.po_date >= '2025-09-22' AND l.gst_rate = 28")[0][0] == 0


def test_generated_data_passes_procurement_rules(generated):
    code, conn = generated
    report = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=RULES + list(INVENTORY_RULES))
    assert {r.rule_id: r.status for r in report.results if r.status != "PASSED"} == {}


_ONE_PO = "(SELECT x FROM (SELECT MIN(purchase_order_id) x FROM purchase_order_headers WHERE po_source = '{}') t)"
_AWARD_PO = _ONE_PO.format("RFQ_AWARD")

_GRN_LINE = "(SELECT x FROM (SELECT MIN(goods_receipt_line_item_id) x FROM goods_inspections WHERE quantity_rejected = 0) t)"

_BILL = "(SELECT x FROM (SELECT MIN(supplier_invoice_id) x FROM supplier_invoices WHERE invoice_status = 'APPROVED') t)"
_TAX_ROW = "(SELECT x FROM (SELECT MIN(tax_transaction_id) x FROM tax_transactions WHERE cgst_amount > 0) t)"

MUTATIONS = {
    "V-PR-12": [f"UPDATE accounts_payable SET original_amount = original_amount + 1, open_amount = open_amount + 1 "
                f"WHERE supplier_invoice_id = {_BILL} AND item_status = 'OPEN'",
                f"UPDATE accounts_payable SET original_amount = original_amount + 1 WHERE supplier_invoice_id = {_BILL} "
                f"AND item_status = 'SETTLED'"],
    "V-PR-13": [f"UPDATE supplier_invoice_line_items SET po_unit_price = po_unit_price + 1 WHERE supplier_invoice_id = {_BILL}"],
    "V-PR-14": [f"UPDATE supplier_invoices SET approved_by_employee_id = (SELECT e FROM (SELECT MIN(employee_id) e "
                f"FROM employees WHERE approval_limit = 0) t) WHERE supplier_invoice_id = {_BILL}"],
    "V-AP-01": [f"UPDATE accounts_payable SET open_amount = open_amount - 1, item_status = 'PARTIALLY_SETTLED' "
                f"WHERE accounts_payable_id = (SELECT x FROM (SELECT MIN(accounts_payable_id) x FROM accounts_payable "
                f"WHERE item_status = 'OPEN') t)"],
    "V-AP-02": ["UPDATE supplier_payment_allocations SET allocation_status = 'REVERSED', reversed_at = NOW() "
                "WHERE supplier_payment_allocation_id = (SELECT x FROM (SELECT MIN(supplier_payment_allocation_id) x "
                "FROM supplier_payment_allocations) t)"],
    "V-TX-01": [f"UPDATE tax_transactions SET taxable_value = taxable_value + 1 WHERE tax_transaction_id = {_TAX_ROW}"],
    "V-TX-02": [f"UPDATE tax_transactions SET cgst_amount = cgst_amount + 1, sgst_amount = sgst_amount + 1 "
                f"WHERE tax_transaction_id = {_TAX_ROW}"],
    "V-PR-08": [f"UPDATE goods_receipt_line_items SET quantity_received = quantity_received + 1 "
                f"WHERE goods_receipt_line_item_id = {_GRN_LINE}"],
    "V-PR-09": ["UPDATE goods_receipts g JOIN purchase_order_headers h USING (purchase_order_id) "
                "SET g.receipt_date = DATE_SUB(h.po_date, INTERVAL 1 DAY), g.received_at = DATE_SUB(h.po_date, INTERVAL 1 DAY) "
                "WHERE g.goods_receipt_id = (SELECT x FROM (SELECT MIN(goods_receipt_id) x FROM goods_receipts) t)"],
    "V-PR-10": ["UPDATE goods_receipt_line_items SET receiving_location_id = (SELECT x FROM (SELECT MIN(warehouse_location_id) x "
                f"FROM warehouse_locations WHERE zone = 'STORAGE') t) WHERE goods_receipt_line_item_id = {_GRN_LINE}"],
    "V-PR-11": ["UPDATE goods_inspections SET quantity_accepted = quantity_accepted - 1, quantity_rejected = 1, "
                "inspection_result = 'PARTIALLY_ACCEPTED', rejection_reason = 'QUALITY_FAILED' "
                f"WHERE goods_receipt_line_item_id = {_GRN_LINE}"],
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


def test_receiving_moves_stock_and_books_grni(generated):
    _, conn = generated
    deliveries = _rows(conn, "SELECT COUNT(*), COUNT(DISTINCT purchase_order_id) FROM goods_receipts")[0]
    assert deliveries[0] > deliveries[1] > 0                        # some POs arrive in several parts
    results = dict(_rows(conn, "SELECT inspection_result, COUNT(*) FROM goods_inspections GROUP BY 1"))
    assert results["ACCEPTED"] > 0 and results.get("PARTIALLY_ACCEPTED", 0) + results.get("REJECTED", 0) > 0
    # Late first deliveries happen, but most arrive on time.
    late = _rows(conn, """SELECT AVG(g.receipt_date > h.expected_delivery_date) FROM goods_receipts g
                          JOIN purchase_order_headers h USING (purchase_order_id)
                          WHERE g.goods_receipt_id IN (SELECT MIN(goods_receipt_id) FROM goods_receipts GROUP BY purchase_order_id)""")[0][0]
    assert 0.05 < late < 0.5
    # P03: GRNI carries the accepted value at PO price (nothing invoiced before 3.7c) and Inventory = ledger.
    receipts = _rows(conn, "SELECT SUM(value_change) FROM inventory_transactions WHERE transaction_type = 'PURCHASE_RECEIPT'")[0][0]
    grni = _rows(conn, "SELECT SUM(l.credit_amount - l.debit_amount) FROM journal_entry_lines l "
                       "JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'GRNI'")[0][0]
    invoiced = _rows(conn, """SELECT COALESCE(SUM(sl.invoiced_quantity * ROUND(sl.po_unit_price * (1 - sl.discount_percent / 100), 4)), 0)
                              FROM supplier_invoice_line_items sl JOIN supplier_invoices i USING (supplier_invoice_id)
                              WHERE i.invoice_status = 'APPROVED'""")[0][0]
    assert receipts > 0 and abs(grni - (receipts - invoiced)) < 1
    # New products get their stock row with the product's reorder policy on first receipt.
    assert _rows(conn, "SELECT COUNT(*) FROM inventory i JOIN products p USING (product_id) "
                       "WHERE p.launch_date > '2025-08-01' AND i.reorder_level = p.reorder_level")[0][0] > 0


def test_bills_ap_and_payments(generated):
    """P04/P05: bills approved into AP; GRNI = received − invoiced at PO price (V-GRNI); weekly payment runs."""
    _, conn = generated
    bills = dict(_rows(conn, "SELECT match_status, COUNT(*) FROM supplier_invoices WHERE invoice_status = 'APPROVED' GROUP BY 1"))
    assert bills["MATCHED"] > 0 and bills.get("PRICE_VARIANCE", 0) > 0
    # V-GRNI: the GRNI balance equals (accepted − invoiced) × PO net price over the PO lines.
    grni = _rows(conn, "SELECT SUM(l.credit_amount - l.debit_amount) FROM journal_entry_lines l "
                       "JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'GRNI'")[0][0]
    uninvoiced = _rows(conn, """
        SELECT SUM(ROUND(t.quantity * t.unit_cost, 2)) - COALESCE((SELECT SUM(ROUND(sl.invoiced_quantity
               * ROUND(sl.po_unit_price * (1 - sl.discount_percent / 100), 4), 2))
               FROM supplier_invoice_line_items sl JOIN supplier_invoices i USING (supplier_invoice_id)
               WHERE i.invoice_status = 'APPROVED'), 0)
        FROM inventory_transactions t WHERE t.transaction_type = 'PURCHASE_RECEIPT'""")[0][0]
    assert grni == uninvoiced
    statuses = dict(_rows(conn, "SELECT payment_status, COUNT(*) FROM supplier_payments GROUP BY 1"))
    assert statuses["COMPLETED"] > 0
    assert _rows(conn, "SELECT COUNT(DISTINCT DAYOFWEEK(payment_date)) FROM supplier_payments")[0][0] == 1   # Fridays
    # MSME suppliers are paid within 45 days of the invoice; some other bills are paid late.
    assert _rows(conn, """SELECT COUNT(*) FROM supplier_payment_allocations x JOIN supplier_payments p USING (supplier_payment_id)
                          JOIN supplier_invoices i USING (supplier_invoice_id) JOIN suppliers s ON s.supplier_id = i.supplier_id
                          WHERE s.msme_category IS NOT NULL AND DATEDIFF(p.payment_date, i.invoice_date) > 45""")[0][0] == 0
    assert _rows(conn, """SELECT COUNT(*) FROM supplier_payment_allocations x JOIN supplier_payments p USING (supplier_payment_id)
                          JOIN supplier_invoices i USING (supplier_invoice_id) WHERE p.payment_date > i.due_date""")[0][0] > 0
    # Input GST only from registered suppliers.
    assert _rows(conn, "SELECT COUNT(*) FROM tax_transactions t JOIN suppliers s USING (supplier_id) "
                       "WHERE s.gst_registration_type <> 'REGISTERED'")[0][0] == 0
