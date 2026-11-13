"""Phase 3 step 3.11: customer returns (requests, approval or rejection, reverse pickup or store drop-off, receipt
into quarantine with P12, inspection to sellable / defective with the defective register, return credit notes),
refunds, and orders closing after their return window, on generated data.

Window: August–November 2025, Small preset. Every returns rule must pass, and each mutation (rolled back) must make
its rule fail.
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
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
RULES = ("V-RT-01", "V-RT-02", "V-RT-03", "V-RT-04", "V-RT-05")
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-11-30"}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zr" + uuid.uuid4().hex[:6]
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


def test_return_requests(generated):
    _, conn = generated
    statuses = dict(_rows(conn, "SELECT return_status, COUNT(*) FROM sales_returns GROUP BY 1"))
    total = sum(statuses.values())
    assert total > 30 and 0 < statuses.get("REJECTED", 0) < total * 0.2
    reasons = {r for (r,) in _rows(conn, "SELECT DISTINCT rejection_reason FROM sales_returns WHERE rejection_reason IS NOT NULL")}
    assert "OUTSIDE_RETURN_WINDOW" in reasons
    # Partial returns: fewer units than delivered on some lines.
    assert _one(conn, """SELECT COUNT(*) FROM sales_return_line_items rl JOIN invoice_line_items il USING (invoice_line_item_id)
                         WHERE rl.quantity_requested < il.quantity""") > 0
    # Store sales come back to their store; shipped orders to the warehouse that shipped them, by reverse pickup.
    assert _one(conn, """SELECT COUNT(*) FROM sales_returns r JOIN orders o USING (order_id)
                         WHERE (o.store_warehouse_id IS NOT NULL) <> (r.return_method = 'DROP_AT_STORE')
                            OR (o.store_warehouse_id IS NOT NULL AND r.return_warehouse_id <> o.store_warehouse_id)
                            OR (r.return_method = 'CARRIER_PICKUP' AND NOT EXISTS (
                                SELECT 1 FROM shipments s WHERE s.order_id = o.order_id AND s.warehouse_id = r.return_warehouse_id))""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM sales_returns r JOIN shipping_carriers c USING (shipping_carrier_id)
                         WHERE NOT c.supports_reverse_pickup""") == 0


def test_received_goods_and_p12(generated):
    _, conn = generated
    # P12 per received return = Σ its RETURN_RECEIPT values (Dr Inventory / Cr COGS).
    assert _one(conn, """SELECT COUNT(*) FROM sales_returns r
                         LEFT JOIN journal_entries j ON j.sales_return_id = r.sales_return_id
                              AND j.journal_source = 'SALES_RETURN_RECEIPT'
                         LEFT JOIN (SELECT rl.sales_return_id, SUM(t.value_change) AS value FROM inventory_transactions t
                                    JOIN sales_return_line_items rl USING (sales_return_line_item_id)
                                    WHERE t.transaction_type = 'RETURN_RECEIPT' GROUP BY 1) v
                              ON v.sales_return_id = r.sales_return_id
                         WHERE r.received_at IS NOT NULL AND (j.journal_entry_id IS NULL OR j.total_debit <> v.value)""") == 0
    results = dict(_rows(conn, "SELECT inspection_result, COUNT(*) FROM return_inspections GROUP BY 1"))
    assert results.get("PASSED", 0) > 0 and results.get("DEFECTIVE", 0) > 0
    assert _one(conn, "SELECT COUNT(*) FROM defective_stock WHERE disposition = 'PENDING' "
                      "AND return_inspection_id IS NOT NULL") == results["DEFECTIVE"]


def test_return_credit_notes(generated):
    _, conn = generated
    # D44: one SALES_RETURN credit note per inspected return × invoice, for the received quantities, no shipping.
    assert _one(conn, """SELECT COUNT(*) FROM sales_returns r WHERE r.return_status IN ('INSPECTED', 'CLOSED')
                         AND NOT EXISTS (SELECT 1 FROM credit_notes n WHERE n.sales_return_id = r.sales_return_id)""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM credit_note_line_items cl JOIN credit_notes n USING (credit_note_id)
                         JOIN invoice_line_items il ON il.invoice_line_item_id = cl.invoice_line_item_id
                         WHERE n.reason = 'SALES_RETURN' AND il.line_type = 'SHIPPING_CHARGE'""") == 0
    # The credit clears what is still open on the invoice (unpaid B2B); a consumer's credit on a paid invoice is
    # refunded: every open consumer credit has a refund on its way.
    assert _one(conn, """SELECT COUNT(*) FROM credit_notes n JOIN accounts_receivable a ON a.credit_note_id = n.credit_note_id
                         JOIN customers k ON k.customer_id = n.customer_id
                         WHERE k.customer_type = 'B2C' AND a.open_amount < 0 AND NOT EXISTS (
                             SELECT 1 FROM refunds r WHERE r.credit_note_id = n.credit_note_id
                               AND r.refund_status IN ('PENDING_APPROVAL', 'APPROVED', 'PROCESSING'))""") == 0


def test_orders_close_after_the_return_window(generated):
    _, conn = generated
    assert _one(conn, "SELECT COUNT(*) FROM orders WHERE order_status = 'CLOSED'") > 500
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN sales_returns r ON r.order_id = o.order_id
                         WHERE o.order_status = 'CLOSED'
                           AND r.return_status NOT IN ('CLOSED', 'REJECTED', 'CANCELLED')
                           AND r.created_at < o.updated_at""") == 0


def test_refunds(generated):
    _, conn = generated
    statuses = dict(_rows(conn, "SELECT refund_status, COUNT(*) FROM refunds GROUP BY 1"))
    assert statuses.get("COMPLETED", 0) > 20
    # To the original card / UPI / net-banking payment; COD by bank transfer; P14 Dr AR / Cr Bank.
    assert _one(conn, """SELECT COUNT(*) FROM refunds r JOIN payment_methods m USING (payment_method_id)
                         LEFT JOIN customer_payments p ON p.customer_payment_id = r.customer_payment_id
                         WHERE (m.method_code IN ('UPI', 'CARD', 'NET_BANKING')) <> (p.customer_payment_id IS NOT NULL)
                            OR p.payment_method_id <> r.payment_method_id""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM refunds r LEFT JOIN journal_entries j ON j.refund_id = r.refund_id
                         WHERE (r.refund_status = 'COMPLETED') <> (j.journal_entry_id IS NOT NULL)""") == 0
    # Business customers get no refund: their credit stays on account for the next invoices.
    assert _one(conn, """SELECT COUNT(*) FROM refunds r JOIN credit_notes n USING (credit_note_id)
                         JOIN customers c ON c.customer_id = n.customer_id WHERE c.customer_type = 'B2B'""") == 0
    # Prepayments of orders cancelled before invoicing go back to source as a reversal (P09).
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments WHERE reversal_reason = 'Refunded: order cancelled'""") > 0
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN orders o USING (order_id)
                         JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
                         WHERE o.order_status = 'CANCELLED' AND p.payment_status = 'SUCCESS'
                           AND a.item_status = 'OPEN' AND o.cancelled_at < '2025-11-20'
                           AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.order_id = o.order_id)""") == 0
    # A return is CLOSED once its credit is refunded or applied.
    assert _one(conn, """SELECT COUNT(*) FROM sales_returns r JOIN credit_notes n ON n.sales_return_id = r.sales_return_id
                         JOIN accounts_receivable a ON a.credit_note_id = n.credit_note_id
                         WHERE r.return_status = 'CLOSED' AND a.open_amount <> 0""") == 0


_FIRST = "(SELECT x FROM (SELECT MIN({key}) x FROM {table} WHERE {where}) t)"
_REJECTED_LINE = _FIRST.format(key="rl.sales_return_line_item_id", table="sales_return_line_items rl JOIN sales_returns r "
                               "USING (sales_return_id)", where="r.return_status = 'REJECTED'")
_APPROVED_LINE = _FIRST.format(key="sales_return_line_item_id", table="sales_return_line_items", where="quantity_approved > 0")
_RECEIPT = _FIRST.format(key="inventory_transaction_id", table="inventory_transactions", where="transaction_type = 'RETURN_RECEIPT'")
_DEFECT = _FIRST.format(key="defective_stock_id", table="defective_stock", where="disposition_status = 'PENDING'")
_REFUND = _FIRST.format(key="refund_id", table="refunds", where="refund_status = 'COMPLETED'")
_RETURN_CREDIT = _FIRST.format(key="credit_note_line_item_id", table="credit_note_line_items",
                               where="sales_return_line_item_id IS NOT NULL")

MUTATIONS = {
    "V-RT-01": [f"UPDATE sales_return_line_items SET quantity_approved = quantity_requested "
                f"WHERE sales_return_line_item_id = {_REJECTED_LINE}"],
    "V-RT-02": [f"UPDATE sales_return_line_items SET quantity_requested = quantity_requested + 100, "
                f"quantity_approved = quantity_approved + 100 WHERE sales_return_line_item_id = {_APPROVED_LINE}"],
    "V-RT-03": [f"UPDATE inventory_transactions SET unit_cost = unit_cost + 1 WHERE inventory_transaction_id = {_RECEIPT}"],
    "V-RT-04": [f"UPDATE refunds SET refund_amount = refund_amount + 100000 WHERE refund_id = {_REFUND}"],
    "V-RT-05": [f"UPDATE defective_stock SET quantity = quantity + 100000 WHERE defective_stock_id = {_DEFECT}"],
    "V-IV-04/return": [f"UPDATE credit_note_line_items SET quantity = quantity + 1 "
                       f"WHERE credit_note_line_item_id = {_RETURN_CREDIT}"],
    "V-IN-05/returns": [f"UPDATE inventory_transactions SET quantity = quantity + 1 WHERE inventory_transaction_id = {_RECEIPT}"],
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
