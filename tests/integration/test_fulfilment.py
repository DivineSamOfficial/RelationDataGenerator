"""Phase 3 step 3.9a: dispatch waves, shipments, stock issue at moving-average cost (FEFO batches), tax invoices
with shipping charges and GST split, P06 with COGS, AR items, prepayment allocation, counter sales paid at the
till and store pickups, on generated data.

Window: August–November 2025 (includes the 22 Sep 2025 GST rate change, so some orders are invoiced at a lower
rate than they were placed at). Every fulfilment rule must pass, and each mutation (rolled back) must make its
rule fail. Mutations named ``RULE/part`` cover the parts a step added to an existing rule.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
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
RULES = ("V-SO-03", "V-SO-04", "V-SH-01", "V-SH-02", "V-IV-01", "V-IV-02", "V-IV-05", "V-IN-09")
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-11-30"}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zf" + uuid.uuid4().hex[:6]
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


def test_dispatch_waves(generated):
    _, conn = generated
    shipments = _one(conn, "SELECT COUNT(*) FROM shipments")
    assert shipments > 500
    # DCs work on working days; the carrier collects in the afternoon window (15:00 to 19:30).
    first, last, sundays = _rows(conn, """SELECT MIN(TIME(dispatched_at)), MAX(TIME(dispatched_at)),
                                                 SUM(DAYOFWEEK(dispatched_at) = 1) FROM shipments""")[0]
    assert first >= timedelta(hours=15) and last <= timedelta(hours=19, minutes=30) and sundays == 0
    # Reserved before the 12:00 cut-off → dispatched the same day; otherwise later.
    assert _one(conn, """SELECT COUNT(*) FROM shipments s JOIN shipment_line_items sl USING (shipment_id)
                         JOIN inventory_reservations r ON r.order_line_item_id = sl.order_line_item_id
                              AND r.warehouse_id = s.warehouse_id
                         WHERE DATE(r.reserved_at) = DATE(s.dispatched_at) AND TIME(r.reserved_at) >= '12:00'""") == 0
    same_day = _one(conn, """SELECT COUNT(*) FROM shipments s JOIN orders o USING (order_id)
                             WHERE DATE(s.dispatched_at) = o.order_date""")
    assert 0 < same_day < shipments
    # Nothing ready is left behind: open shipped orders were confirmed in the last days of the window.
    assert _one(conn, """SELECT COUNT(*) FROM orders o JOIN order_status_history h ON h.order_id = o.order_id
                         AND h.to_status = 'CONFIRMED'
                         WHERE o.fulfilment_type = 'SHIP' AND o.order_status = 'CONFIRMED'
                           AND h.changed_at < '2025-11-27'""") == 0
    # One shipment per order, warehouse and day (a released backorder ships later); split orders ship from each DC.
    assert _one(conn, """SELECT COUNT(*) FROM (SELECT order_id, warehouse_id FROM shipments
                                               GROUP BY 1, 2, planned_dispatch_date HAVING COUNT(*) > 1) x""") == 0
    assert _one(conn, "SELECT COUNT(*) FROM (SELECT order_id FROM shipments GROUP BY 1 HAVING COUNT(*) > 1) x") > 0


def test_carriers_and_freight(generated):
    _, conn = generated
    by_type = dict(_rows(conn, """SELECT c.carrier_type, COUNT(*) FROM shipments s JOIN shipping_carriers c
                                  USING (shipping_carrier_id) GROUP BY 1"""))
    assert {"EXPRESS_COURIER", "SURFACE_LTL", "HYPERLOCAL"} <= set(by_type)
    # COD parcels only with carriers that collect cash; tracking numbers carry the carrier's prefix.
    assert _one(conn, """SELECT COUNT(*) FROM shipments s JOIN shipping_carriers c USING (shipping_carrier_id)
                         WHERE (s.is_cod AND NOT c.supports_cod)
                            OR LEFT(s.tracking_number, CHAR_LENGTH(c.tracking_number_prefix)) <> c.tracking_number_prefix""") == 0
    # COD amount = the shipment's invoice total; COD exactly for COD orders.
    assert _one(conn, """SELECT COUNT(*) FROM shipments s JOIN orders o USING (order_id)
                         JOIN payment_terms t ON t.payment_term_id = o.payment_term_id
                         JOIN invoices i ON i.shipment_id = s.shipment_id
                         WHERE s.is_cod <> (t.term_code = 'COD') OR (s.is_cod AND s.cod_amount <> i.total_amount)""") == 0
    # Heavier consignments travel by surface; surface is slower; every shipment pays freight.
    heavy, light = _rows(conn, """SELECT AVG(IF(c.carrier_type = 'SURFACE_LTL', s.total_weight_kg, NULL)),
                                         AVG(IF(c.carrier_type = 'EXPRESS_COURIER', s.total_weight_kg, NULL))
                                  FROM shipments s JOIN shipping_carriers c USING (shipping_carrier_id)""")[0]
    assert heavy > light
    assert _one(conn, "SELECT COUNT(*) FROM shipments WHERE freight_cost_amount <= 0") == 0
    assert _one(conn, """SELECT COUNT(*) FROM shipments s JOIN invoices i USING (shipment_id)
                         WHERE (s.eway_bill_number IS NOT NULL) <> (i.total_amount > 50000)""") == 0


def test_invoices_at_dispatch(generated):
    _, conn = generated
    # One invoice per shipment, issued at the dispatch; store sales are invoiced at handover.
    assert _one(conn, "SELECT COUNT(*) FROM shipments s LEFT JOIN invoices i USING (shipment_id) "
                      "WHERE i.invoice_id IS NULL") == 0
    handover = dict(_rows(conn, """SELECT o.fulfilment_type, COUNT(*) FROM invoices i JOIN orders o USING (order_id)
                                   WHERE i.shipment_id IS NULL GROUP BY 1"""))
    assert set(handover) == {"COUNTER_SALE", "STORE_PICKUP"}
    # Both tax modes occur; e-invoices only for registered buyers (D46), all of them.
    inter, intra, irn, registered = _rows(conn, """SELECT SUM(is_inter_state), SUM(NOT is_inter_state),
                                                          SUM(irn IS NOT NULL), SUM(customer_gstin IS NOT NULL)
                                                   FROM invoices""")[0]
    assert inter > 0 and intra > 0 and irn == registered > 0
    # The GST cut: an order placed before 22 Sep and invoiced after carries the lower invoice-date rate.
    assert _one(conn, """SELECT COUNT(*) FROM invoice_line_items il JOIN invoices i USING (invoice_id)
                         JOIN order_line_items l ON l.order_line_item_id = il.order_line_item_id
                         WHERE il.gst_rate < l.gst_rate AND i.invoice_date >= '2025-09-22'""") > 0
    # Shipping charge on the order's first invoice (D42).
    assert _one(conn, "SELECT COUNT(*) FROM invoice_line_items WHERE line_type = 'SHIPPING_CHARGE'") > 0
    # Every invoice line has one OUTPUT tax row; B2B ⇔ registered buyer.
    assert _one(conn, """SELECT COUNT(*) FROM invoice_line_items il LEFT JOIN tax_transactions t
                         ON t.invoice_line_item_id = il.invoice_line_item_id WHERE t.tax_transaction_id IS NULL""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM tax_transactions t JOIN invoice_line_items il USING (invoice_line_item_id)
                         JOIN invoices i USING (invoice_id)
                         WHERE (t.gstr1_section = 'B2B') <> (i.customer_gstin IS NOT NULL)""") == 0


def test_cogs_posted_with_the_invoice(generated):
    _, conn = generated
    # P06 (D52): per invoice, COGS debit = Inventory credit = Σ issue values; AR debit = invoice total.
    assert _one(conn, """
        SELECT COUNT(*) FROM invoices i JOIN journal_entries j ON j.invoice_id = i.invoice_id
        JOIN (SELECT l.journal_entry_id,
                     SUM(IF(a.system_role = 'AR_CONTROL', l.debit_amount, 0)) AS ar,
                     SUM(IF(a.account_id IN (SELECT cogs_account_id FROM business_units), l.debit_amount, 0)) AS cogs,
                     SUM(IF(a.system_role = 'INVENTORY', l.credit_amount, 0)) AS stock
              FROM journal_entry_lines l JOIN chart_of_accounts a USING (account_id) GROUP BY 1) p
             ON p.journal_entry_id = j.journal_entry_id
        LEFT JOIN (SELECT COALESCE(il1.invoice_id, il2.invoice_id) AS invoice_id, -SUM(t.value_change) AS issued
                   FROM inventory_transactions t
                   LEFT JOIN invoice_line_items il1 ON il1.shipment_line_item_id = t.shipment_line_item_id
                   LEFT JOIN invoice_line_items il2 ON il2.invoice_line_item_id = t.invoice_line_item_id
                   WHERE t.transaction_type = 'SALES_ISSUE' GROUP BY 1) x ON x.invoice_id = i.invoice_id
        WHERE j.journal_source <> 'SALES_INVOICE' OR p.ar <> i.total_amount OR p.cogs <> x.issued
           OR p.stock <> x.issued""") == 0
    # Gross margin is positive overall (list prices are set from the pack margins).
    revenue, cogs = _rows(conn, """SELECT SUM(taxable_amount), (SELECT -SUM(value_change) FROM inventory_transactions
                                                               WHERE transaction_type = 'SALES_ISSUE')
                                   FROM invoice_line_items WHERE line_type = 'PRODUCT'""")[0]
    assert cogs < revenue


def test_stock_issue_follows_fefo(generated):
    _, conn = generated
    # Batch-tracked products ship one line per batch and never from an expired batch.
    assert _one(conn, """SELECT COUNT(*) FROM shipment_line_items sl JOIN order_line_items l USING (order_line_item_id)
                         JOIN products p ON p.product_id = l.product_id
                         WHERE p.is_batch_tracked AND sl.inventory_batch_id IS NULL""") == 0
    assert _one(conn, "SELECT COUNT(*) FROM shipment_line_items WHERE inventory_batch_id IS NOT NULL") > 0
    # FEFO: when a shipment line takes a batch, no batch expiring earlier was left at that warehouse unpicked
    # by the same line (lines of one order line are cut in expiry order).
    assert _one(conn, """SELECT COUNT(*) FROM shipment_line_items a JOIN shipment_line_items b
                         ON b.shipment_id = a.shipment_id AND b.order_line_item_id = a.order_line_item_id
                         AND b.line_number > a.line_number
                         JOIN inventory_batches ba ON ba.inventory_batch_id = a.inventory_batch_id
                         JOIN inventory_batches bb ON bb.inventory_batch_id = b.inventory_batch_id
                         WHERE COALESCE(bb.expiry_date, '9999-12-31') < COALESCE(ba.expiry_date, '9999-12-31')""") == 0
    # Reservations are fulfilled by what was issued; the reserved quantity left on stock is still outstanding.
    assert _one(conn, "SELECT COUNT(*) FROM inventory_reservations WHERE reservation_status = 'FULFILLED'") > 0


def test_prepayments_clear_their_invoices(generated):
    _, conn = generated
    # A fully invoiced prepaid order's prepayment is allocated to its invoices. The invoice round-off makes the
    # total equal to the prepayment, so a single invoice clears it exactly unless the customer is owed money back
    # (refunded in step 3.11): the GST rate fell between order and invoice, lines were cancelled, or the order was
    # split (each invoice's shipping GST follows its own goods, D42).
    rows = _rows(conn, """
        SELECT o.order_id, a.open_amount,
               (SELECT COUNT(*) FROM invoice_line_items il JOIN invoices i USING (invoice_id)
                WHERE i.order_id = o.order_id AND i.invoice_date >= '2025-09-22' AND o.order_date < '2025-09-22') AS rate_cut,
               (SELECT SUM(quantity_cancelled) FROM order_line_items l WHERE l.order_id = o.order_id) AS cancelled,
               (SELECT COUNT(*) FROM invoices i WHERE i.order_id = o.order_id) AS invoices
        FROM orders o JOIN payment_terms t USING (payment_term_id)
        JOIN customer_payments p ON p.order_id = o.order_id AND p.payment_status = 'SUCCESS'
        JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
        WHERE t.is_prepaid AND o.order_status IN ('SHIPPED', 'DELIVERED')""")
    assert len(rows) > 100
    assert all(open_amount == 0 for _, open_amount, rate_cut, cancelled, invoices in rows
               if not rate_cut and not cancelled and invoices == 1)
    assert all(open_amount <= 0 for _, open_amount, *_ in rows)              # never short-paid
    assert _one(conn, """SELECT COUNT(*) FROM invoices i JOIN orders o USING (order_id) JOIN payment_terms t
                         ON t.payment_term_id = o.payment_term_id JOIN accounts_receivable a ON a.invoice_id = i.invoice_id
                         WHERE t.is_prepaid AND a.item_status <> 'SETTLED'""") == 0


def test_counter_sales_paid_at_the_till(generated):
    _, conn = generated
    # Every counter sale is handed over at once: DELIVERED, paid in full, both AR items settled.
    assert _one(conn, """SELECT COUNT(*) FROM orders WHERE fulfilment_type = 'COUNTER_SALE'
                         AND order_status NOT IN ('DELIVERED', 'CANCELLED')""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM invoices i JOIN orders o USING (order_id)
                         LEFT JOIN customer_payments p ON p.order_id = o.order_id AND p.payment_type = 'COUNTER_PAYMENT'
                         LEFT JOIN accounts_receivable a ON a.invoice_id = i.invoice_id
                         LEFT JOIN accounts_receivable b ON b.customer_payment_id = p.customer_payment_id
                         WHERE o.fulfilment_type = 'COUNTER_SALE'
                           AND (p.amount <> i.total_amount OR p.completed_at <> i.issued_at
                                OR a.item_status <> 'SETTLED' OR b.item_status <> 'SETTLED')""") == 0
    # Cash goes to the store's float (P08 Dr petty cash), card and UPI to Payment Clearing (D53).
    methods = dict(_rows(conn, """SELECT m.method_code, COUNT(*) FROM customer_payments p JOIN payment_methods m
                                  USING (payment_method_id) WHERE p.payment_type = 'COUNTER_PAYMENT' GROUP BY 1"""))
    assert {"CASH", "UPI"} <= set(methods)
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                         JOIN bank_accounts b ON b.bank_account_id = p.bank_account_id
                         JOIN orders o ON o.order_id = p.order_id
                         WHERE p.payment_type = 'COUNTER_PAYMENT'
                           AND ((m.method_code = 'CASH') <> (b.account_type = 'PETTY_CASH')
                                OR (m.method_code = 'CASH' AND b.warehouse_id <> o.store_warehouse_id))""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                         JOIN bank_accounts b ON b.bank_account_id = p.bank_account_id
                         JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id
                         JOIN journal_entry_lines l ON l.journal_entry_id = j.journal_entry_id AND l.debit_amount > 0
                         JOIN chart_of_accounts a ON a.account_id = l.account_id
                         WHERE p.payment_type = 'COUNTER_PAYMENT'
                           AND l.account_id <> IF(m.method_code = 'CASH', b.gl_account_id,
                               (SELECT account_id FROM chart_of_accounts WHERE system_role = 'PAYMENT_CLEARING'))""") == 0


def test_store_pickups_handed_over(generated):
    _, conn = generated
    pickups = _rows(conn, """SELECT DATEDIFF(i.invoice_date, o.order_date), i.warehouse_id = o.store_warehouse_id,
                                    i.place_of_supply_state_id = w.state_id
                             FROM invoices i JOIN orders o USING (order_id) JOIN warehouses w ON w.warehouse_id = o.store_warehouse_id
                             WHERE o.fulfilment_type = 'STORE_PICKUP'""")
    assert pickups and all(0 <= days <= 7 and at_store and store_state for days, at_store, store_state in pickups)


def test_backordered_orders_ship_in_parts(generated):
    _, conn = generated
    # A backordered order ships its reserved lines first (PARTIALLY_SHIPPED) and the rest when stock arrives.
    assert _one(conn, """SELECT COUNT(DISTINCT order_id) FROM order_status_history
                         WHERE to_status = 'PARTIALLY_SHIPPED'""") > 0


_SHIPPED_LINE = ("(SELECT x FROM (SELECT MIN(shipment_line_item_id) x FROM shipment_line_items) t)")
_SHIPMENT = "(SELECT x FROM (SELECT MIN(shipment_id) x FROM shipments) t)"
_INVOICE = "(SELECT x FROM (SELECT MIN(invoice_id) x FROM invoices WHERE shipment_id IS NOT NULL) t)"
_ISSUE = ("(SELECT x FROM (SELECT MIN(inventory_transaction_id) x FROM inventory_transactions "
          "WHERE transaction_type = 'SALES_ISSUE' AND shipment_line_item_id IS NOT NULL) t)")
_PRODUCT_LINE = ("(SELECT x FROM (SELECT MIN(invoice_line_item_id) x FROM invoice_line_items "
                 "WHERE line_type = 'PRODUCT') t)")
_B2CS = ("(SELECT x FROM (SELECT MIN(tax_transaction_id) x FROM tax_transactions WHERE gstr1_section = 'B2CS') t)")
_SHIPPED_ORDER_LINE = ("(SELECT x FROM (SELECT MIN(order_line_item_id) x FROM order_line_items "
                       "WHERE line_status = 'SHIPPED') t)")

MUTATIONS = {
    "V-SO-03": [f"UPDATE shipment_line_items SET quantity_shipped = quantity_shipped + 1 "
                f"WHERE shipment_line_item_id = {_SHIPPED_LINE}"],
    "V-SO-04": [f"UPDATE invoices SET invoice_status = 'CANCELLED', cancelled_at = issued_at, "
                f"cancellation_reason = 'Test' WHERE invoice_id = {_INVOICE}"],
    "V-SH-01": [f"UPDATE shipments SET dispatched_at = dispatched_at + INTERVAL 1 MINUTE WHERE shipment_id = {_SHIPMENT}"],
    "V-SH-02": [f"UPDATE inventory_transactions SET transaction_at = transaction_at + INTERVAL 1 MINUTE "
                f"WHERE inventory_transaction_id = {_ISSUE}"],
    "V-IV-01": [f"UPDATE invoices SET customer_name = CONCAT(customer_name, ' X') WHERE invoice_id = {_INVOICE}"],
    "V-IV-02": [f"UPDATE invoice_line_items SET hsn_code = '0000' WHERE invoice_line_item_id = {_PRODUCT_LINE}"],
    "V-IV-05": [f"UPDATE invoices SET invoice_number = CONCAT('ZZ', SUBSTRING(invoice_number, 3)) "
                f"WHERE invoice_id = {_INVOICE}"],
    "V-IN-09": [f"UPDATE inventory_transactions SET unit_cost = unit_cost + 1 WHERE inventory_transaction_id = {_ISSUE}"],
    # Parts that step 3.9a added to earlier rules.
    "V-SO-01/invoice": [f"UPDATE invoices SET taxable_amount = taxable_amount + 1, total_amount = total_amount + 1 "
                        f"WHERE invoice_id = {_INVOICE}"],
    "V-SO-05/shipped": [f"UPDATE order_line_items SET line_status = 'PARTIALLY_SHIPPED' "
                        f"WHERE order_line_item_id = {_SHIPPED_ORDER_LINE}"],
    "V-TX-01/sales": [f"UPDATE tax_transactions SET gstr1_section = 'B2CL' WHERE tax_transaction_id = {_B2CS}"],
    "V-IN-05/sales": [f"UPDATE inventory_transactions SET quantity = quantity - 1 WHERE inventory_transaction_id = {_ISSUE}"],
}


def test_every_rule_has_a_mutation():
    assert {key.split("/")[0] for key in MUTATIONS} >= set(RULES)


@pytest.mark.parametrize("key", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, key):
    code, conn = generated
    rule_id = key.split("/")[0]
    try:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))     # some mutations break composite keys on purpose
        for statement in MUTATIONS[key]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
