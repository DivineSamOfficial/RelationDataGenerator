"""Phase 3 step 3.8: customer orders from the demand model, prepaid checkout (P08 + AR), COD and counter
sales, B2B credit checks and holds (3.8a); stock reservations, backorders, customer cancellations, new customers
and replenishment of backordered demand (3.8b), on generated data.

Window: August–November 2025 (includes the 22 Sep 2025 GST rate change and the festive campaigns). After four
weeks the fixture lowers the credit limit of a few busy B2B accounts (a credit-control decision), so holds,
Finance overrides and expired holds all occur. Every sales and receivables rule must pass, and each mutation
(rolled back) must make its rule fail. Backordered lines are cancelled after 7 days here (21 by default), so
the out-of-stock path occurs in a short window.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

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
RULES = sorted(r for r in framework.registered_checks() if r.startswith(("V-SO", "V-PY", "V-AR")) or r == "V-IN-03")
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-11-30", "sales.backorder_cancel_days": 7}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zs" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty", "fmcg"],
                     base_seed=7)
    try:
        config = load_generation_config(preset="small", today=date(2026, 11, 10), overrides=WINDOW)
        generate_history(settings, code, config, days=28)
        engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
        with engine.connect() as conn:
            conn.execute(text("""
                UPDATE customers c JOIN (SELECT customer_id FROM orders GROUP BY customer_id
                                         ORDER BY SUM(items_taxable_amount) DESC LIMIT 6) busy USING (customer_id)
                SET c.credit_limit = 50000, c.updated_at = '2025-08-28 23:59:59' WHERE c.customer_type = 'B2B' AND c.credit_limit IS NOT NULL"""))
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


def _one(conn, sql):
    return conn.execute(text(sql)).scalar()


def test_demand_follows_the_model(generated):
    _, conn = generated
    by_type = dict(_rows(conn, """SELECT c.customer_type, SUM(o.items_taxable_amount) FROM orders o
                                  JOIN customers c USING (customer_id) WHERE o.order_status <> 'CANCELLED' GROUP BY 1"""))
    share = by_type["B2B"] / (by_type["B2B"] + by_type["B2C"])
    assert Decimal("0.65") < share < Decimal("0.92"), share                  # D23: ~80% of value from B2B
    days = (date(2025, 11, 30) - date(2025, 8, 1)).days + 1
    per_day = _one(conn, "SELECT COUNT(*) FROM orders") / days
    assert 10 < per_day < 30, per_day                                          # Small preset: 14 a day before uplifts
    # Weekly rhythm: B2B buys on working days; consumers buy more at the weekend and in the evening.
    b2b_sunday = _one(conn, """SELECT AVG(DAYOFWEEK(o.order_date) = 1) FROM orders o JOIN customers c USING (customer_id)
                               WHERE c.customer_type = 'B2B'""")
    assert b2b_sunday < Decimal("0.05")
    evening, morning = _rows(conn, """SELECT SUM(HOUR(order_placed_at) BETWEEN 19 AND 22), SUM(HOUR(order_placed_at) BETWEEN 6 AND 9)
                                      FROM orders WHERE prices_include_tax AND fulfilment_type <> 'COUNTER_SALE'""")[0]
    assert evening > 2 * morning
    # The festive season (October: higher seasonality and campaign uplift) sells more than August.
    months = dict(_rows(conn, "SELECT MONTH(order_date), COUNT(*) FROM orders GROUP BY 1"))
    assert months[10] > months[8] * Decimal("1.2")
    campaign = _one(conn, "SELECT COUNT(*) FROM orders WHERE marketing_campaign_id IS NOT NULL")
    assert campaign > 0
    # Counter sales happen in stores, during store hours, on working days.
    counters = _rows(conn, """SELECT MIN(HOUR(o.order_placed_at)), MAX(HOUR(o.order_placed_at)), SUM(DAYOFWEEK(o.order_date) = 1),
                                     COUNT(*), SUM(w.warehouse_type <> 'RETAIL_STORE') FROM orders o
                              JOIN warehouses w ON w.warehouse_id = o.store_warehouse_id WHERE o.fulfilment_type = 'COUNTER_SALE'""")[0]
    assert counters[3] > 0 and counters[0] >= 10 and counters[1] <= 21 and counters[2] == 0 and counters[4] == 0


def test_prices_follow_the_customer_type(generated):
    _, conn = generated
    # B2C: whole-rupee shelf prices incl. GST (D79); B2B: list price excluding GST.
    assert _one(conn, """SELECT COUNT(*) FROM order_line_items l JOIN orders o USING (order_id)
                         WHERE o.prices_include_tax AND l.inclusive_unit_price <> ROUND(l.inclusive_unit_price, 0)""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM order_line_items l JOIN orders o USING (order_id)
                         WHERE NOT o.prices_include_tax AND l.unit_price <> l.list_price""") == 0
    # The 22 Sep 2025 GST cut: orders before and after it carry different rates for the same product.
    changed = _one(conn, """SELECT COUNT(DISTINCT l.product_id) FROM order_line_items l JOIN orders o USING (order_id)
                            JOIN order_line_items l2 ON l2.product_id = l.product_id JOIN orders o2 ON o2.order_id = l2.order_id
                            WHERE o.order_date < '2025-09-22' AND o2.order_date >= '2025-09-22' AND l.gst_rate > l2.gst_rate""")
    assert changed > 0
    discounts = dict(_rows(conn, "SELECT discount_type, COUNT(*) FROM order_line_items WHERE discount_type IS NOT NULL GROUP BY 1"))
    assert {"CAMPAIGN_PROMO", "SEGMENT", "VOLUME"} <= set(discounts)
    # Shipping charge only below the free-shipping threshold (incl. GST) on shipped B2C orders (D42).
    assert _one(conn, """SELECT COUNT(*) FROM orders WHERE shipping_charge_amount > 0
                         AND (NOT prices_include_tax OR items_taxable_amount + items_tax_amount >= 499)""") == 0


def test_prepaid_checkout_posts_receipts(generated):
    _, conn = generated
    attempts, failed = _rows(conn, "SELECT COUNT(*), SUM(payment_status = 'FAILED') FROM customer_payments")[0]
    assert Decimal("0.03") < failed / attempts < Decimal("0.15")              # ~8% per attempt (D5)
    assert _one(conn, "SELECT COUNT(*) FROM orders WHERE cancellation_reason = 'PAYMENT_FAILED'") > 0
    assert _one(conn, "SELECT COUNT(*) FROM orders WHERE order_status IN ('PENDING_PAYMENT', 'PENDING_CREDIT_CHECK')") == 0
    # Every successful receipt: P08 (Dr Payment Clearing / Cr AR) and an unapplied AR item (D49, D53).
    successes, journals, items = _rows(conn, """
        SELECT COUNT(*), COUNT(j.journal_entry_id), COUNT(a.accounts_receivable_id) FROM customer_payments p
        LEFT JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id AND j.journal_source = 'CUSTOMER_PAYMENT'
        LEFT JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
        WHERE p.payment_status = 'SUCCESS'""")[0]
    assert successes > 0 and successes == journals == items
    assert _one(conn, """SELECT COUNT(*) FROM journal_entries j JOIN customer_payments p USING (customer_payment_id)
                         WHERE p.payment_status <> 'SUCCESS'""") == 0
    # V-CLR: Payment Clearing = successful gateway receipts not yet settled by the bank (settlement comes in 3.14).
    clearing = _one(conn, """SELECT SUM(l.debit_amount - l.credit_amount) FROM journal_entry_lines l
                             JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'PAYMENT_CLEARING'""")
    unsettled = _one(conn, """SELECT SUM(p.amount) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                              WHERE p.payment_status = 'SUCCESS' AND p.settlement_bank_transaction_id IS NULL
                                AND m.method_code IN ('UPI', 'CARD', 'NET_BANKING')""")
    assert clearing == unsettled
    # Gateway fee = amount × the method's fee %, settlement date = payment date + the method's lag.
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                         WHERE p.gateway_fee_amount <> ROUND(p.amount * COALESCE(m.gateway_fee_percent, 0) / 100, 2)
                            OR (p.payment_status = 'SUCCESS'
                                AND p.settlement_date <> DATE_ADD(p.payment_date, INTERVAL m.settlement_lag_days DAY))""") == 0


def test_payment_terms_follow_the_checkout(generated):
    _, conn = generated
    terms = {(kind, fulfilment, term) for kind, fulfilment, term in _rows(conn, """
        SELECT c.customer_type, o.fulfilment_type, t.term_code FROM orders o JOIN customers c USING (customer_id)
        JOIN payment_terms t ON t.payment_term_id = o.payment_term_id GROUP BY 1, 2, 3""")}
    assert ("B2C", "SHIP", "COD") in terms and ("B2C", "SHIP", "ADVANCE") in terms
    assert all(term == "IMMEDIATE" for kind, fulfilment, term in terms if fulfilment == "COUNTER_SALE")
    assert all(term == "ADVANCE" for kind, fulfilment, term in terms if fulfilment == "STORE_PICKUP")
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN payment_terms t USING (payment_term_id)
                         WHERE t.term_code = 'COD' AND o.order_total_estimate > 50000""") == 0


def test_credit_holds(generated):
    _, conn = generated
    held = dict(_rows(conn, """SELECT o.order_status, COUNT(DISTINCT o.order_id) FROM orders o
                               JOIN order_status_history h ON h.order_id = o.order_id AND h.to_status = 'ON_HOLD' GROUP BY 1"""))
    assert sum(held.values()) > 0
    reasons = dict(_rows(conn, """SELECT change_reason, COUNT(*) FROM order_status_history
                                  WHERE from_status = 'ON_HOLD' GROUP BY 1"""))
    assert reasons.get("Finance override", 0) > 0
    assert any(r.startswith("Credit hold expired") for r in reasons)
    assert _one(conn, "SELECT COUNT(*) FROM orders WHERE cancellation_reason = 'CREDIT_HOLD_EXPIRED'") > 0
    # Only B2B orders on credit go through the credit check.
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN customers c USING (customer_id) JOIN order_status_history h
                         ON h.order_id = o.order_id WHERE h.to_status = 'ON_HOLD' AND c.customer_type <> 'B2B'""") == 0


def test_confirmed_orders_reserve_stock(generated):
    _, conn = generated
    # Every open line of a confirmed order is reserved in full; backordered lines have no reservation.
    assert _one(conn, """SELECT COUNT(*) FROM order_line_items l JOIN orders o USING (order_id)
                         LEFT JOIN (SELECT order_line_item_id, SUM(outstanding_quantity) AS q FROM inventory_reservations
                                    GROUP BY 1) r ON r.order_line_item_id = l.order_line_item_id
                         WHERE o.order_status IN ('CONFIRMED', 'BACKORDERED')
                           AND ((l.line_status = 'OPEN' AND COALESCE(r.q, 0) <> l.quantity_ordered)
                             OR (l.line_status = 'BACKORDERED' AND COALESCE(r.q, 0) <> 0))""") == 0
    # Shipped orders reserve at distribution centres (several when the nearest is short), store orders at the store.
    assert _one(conn, """SELECT COUNT(*) FROM inventory_reservations r JOIN order_line_items l USING (order_line_item_id)
                         JOIN orders o USING (order_id) JOIN warehouses w ON w.warehouse_id = r.warehouse_id
                         WHERE (o.fulfilment_type = 'SHIP') <> (w.warehouse_type = 'DISTRIBUTION_CENTRE')
                            OR (o.fulfilment_type <> 'SHIP' AND r.warehouse_id <> o.store_warehouse_id)""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM (SELECT order_line_item_id FROM inventory_reservations GROUP BY 1
                                               HAVING COUNT(*) > 1) x""") > 0
    # Cancelled orders hold nothing back.
    assert _one(conn, """SELECT COUNT(*) FROM inventory_reservations r JOIN order_line_items l USING (order_line_item_id)
                         JOIN orders o USING (order_id) WHERE o.order_status = 'CANCELLED'
                           AND r.reservation_status <> 'RELEASED'""") == 0


def test_backorders_wait_for_stock(generated):
    _, conn = generated
    ever = _one(conn, "SELECT COUNT(DISTINCT order_id) FROM order_status_history WHERE to_status = 'BACKORDERED'")
    total = _one(conn, "SELECT COUNT(*) FROM orders")
    assert 0 < ever < total * Decimal("0.10")                                  # ~3% by design
    outcomes = {reason for (reason,) in _rows(conn, "SELECT DISTINCT change_reason FROM order_status_history "
                                                    "WHERE from_status = 'BACKORDERED'")}
    assert "Backorder released: stock reserved" in outcomes
    assert outcomes & {"Out of stock for 7 days", "Backordered lines cancelled: out of stock"}
    # The stock check orders backordered demand: every line backordered before the last day has supply coming.
    assert _one(conn, """SELECT COUNT(*) FROM order_line_items l JOIN orders o USING (order_id)
                         WHERE l.line_status = 'BACKORDERED' AND o.fulfilment_type = 'SHIP' AND o.updated_at < '2025-11-30'
                           AND NOT EXISTS (SELECT 1 FROM purchase_order_line_items pl JOIN purchase_order_headers h
                                           USING (purchase_order_id) WHERE pl.product_id = l.product_id
                                           AND h.po_status IN ('PENDING_APPROVAL', 'APPROVED', 'SENT', 'PARTIALLY_RECEIVED'))
                           AND NOT EXISTS (SELECT 1 FROM rfq_line_items rl JOIN rfq_headers r USING (rfq_id)
                                           WHERE rl.product_id = l.product_id AND r.rfq_status IN ('ISSUED', 'UNDER_EVALUATION'))""") == 0


def test_customer_cancellations(generated):
    _, conn = generated
    reasons = dict(_rows(conn, "SELECT cancellation_reason, COUNT(*) FROM orders WHERE order_status = 'CANCELLED' GROUP BY 1"))
    assert reasons.get("CUSTOMER_REQUEST", 0) > 0 and reasons.get("DUPLICATE_ORDER", 0) > 0
    # Customers cancel before dispatch, never at the counter, and after the order's last earlier status.
    assert _one(conn, """SELECT COUNT(*) FROM orders WHERE cancellation_reason IN ('CUSTOMER_REQUEST', 'DUPLICATE_ORDER')
                         AND (fulfilment_type = 'COUNTER_SALE' OR cancelled_at <= order_placed_at)""") == 0


def test_new_customers_join_and_order(generated):
    _, conn = generated
    new, credited = _rows(conn, """SELECT COUNT(*), SUM(acquisition_marketing_campaign_id IS NOT NULL) FROM customers
                                   WHERE acquisition_date >= '2025-08-01'""")[0]
    assert new > 0 and credited > 0
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN customers c USING (customer_id)
                         WHERE c.acquisition_date >= '2025-08-01'""") > 0
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN customers c USING (customer_id)
                         WHERE o.order_date <= c.acquisition_date AND c.acquisition_date >= '2025-08-01'""") == 0


_ORDER = "(SELECT x FROM (SELECT MIN(order_id) x FROM orders WHERE order_status = 'CONFIRMED') t)"
_B2C = ("(SELECT x FROM (SELECT MIN(o.order_id) x FROM orders o JOIN customers c USING (customer_id) "
        "WHERE c.customer_type = 'B2C') t)")
_CHECKED = ("(SELECT x FROM (SELECT MIN(o.customer_id) x FROM orders o JOIN order_status_history h USING (order_id) "
            "JOIN customers c ON c.customer_id = o.customer_id WHERE h.change_reason = 'Credit check passed' "
            "AND c.credit_limit IS NOT NULL) t)")
_PAYMENT = "(SELECT x FROM (SELECT MIN(customer_payment_id) x FROM customer_payments WHERE payment_status = 'SUCCESS') t)"
_ITEM = "(SELECT x FROM (SELECT MIN(accounts_receivable_id) x FROM accounts_receivable) t)"

MUTATIONS = {
    "V-SO-01": [f"UPDATE orders SET items_taxable_amount = items_taxable_amount + 1, "
                f"order_total_estimate = order_total_estimate + 1 WHERE order_id = {_ORDER}"],
    "V-SO-02": [f"UPDATE orders SET sales_channel_id = (SELECT sales_channel_id FROM sales_channels "
                f"WHERE channel_code = 'FIELD_SALES') WHERE order_id = {_B2C}"],
    "V-SO-05": [f"UPDATE orders SET order_status = 'CANCELLED', cancelled_at = order_placed_at, "
                f"cancellation_reason = 'CUSTOMER_REQUEST' WHERE order_id = {_ORDER}"],
    "V-SO-06": [f"UPDATE customers SET credit_limit = 1 WHERE customer_id = {_CHECKED}"],
    "V-PY-02": [f"UPDATE customer_payments SET amount = amount - 1 WHERE customer_payment_id = {_PAYMENT}"],
    "V-AR-01": [f"UPDATE accounts_receivable SET original_amount = original_amount + 1, open_amount = open_amount + 1 "
                f"WHERE accounts_receivable_id = {_ITEM}"],
    "V-AR-02": [f"UPDATE accounts_receivable SET customer_id = customer_id + 1 WHERE accounts_receivable_id = {_ITEM}"],
    "V-IN-03": ["UPDATE inventory SET qty_reserved = qty_reserved + 1 WHERE (warehouse_id, product_id) = "
                "(SELECT w, p FROM (SELECT r.warehouse_id w, l.product_id p FROM inventory_reservations r "
                "JOIN order_line_items l USING (order_line_item_id) WHERE r.reservation_status = 'ACTIVE' "
                "ORDER BY r.inventory_reservation_id LIMIT 1) t)"],
    "V-AR-03": [f"UPDATE accounts_receivable SET open_amount = open_amount + 1, item_status = 'PARTIALLY_SETTLED' "
                f"WHERE accounts_receivable_id = {_ITEM}"],
}


def test_every_rule_has_a_mutation():
    assert set(MUTATIONS) == set(RULES)


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, rule_id):
    code, conn = generated
    try:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))     # some mutations break composite keys on purpose
        for statement in MUTATIONS[rule_id]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
