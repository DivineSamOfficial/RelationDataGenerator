"""Scenario distribution report (master prompt §6; 1G §2.3; step 3.15).

Each master-prompt scenario is measured on a workspace as a share (numerator / denominator, SQL below) and compared
with the plausible band in ``config/scenarios.yaml``. The rates that *produce* the scenarios live in
``config/generation.yaml`` (and the industry packs); ``scenarios.yaml`` names them per scenario, so the report says
which knob to turn when a share is outside its band. ``inventory_adjustments`` is a count per month, not a share.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

# scenario → (numerator SQL, denominator SQL); each returns one number.
MEASURES: dict[str, tuple[str, str]] = {
    "successful_deliveries": (
        "SELECT COUNT(*) FROM orders WHERE order_status IN ('DELIVERED', 'CLOSED')",
        "SELECT COUNT(*) FROM orders"),
    "partial_deliveries": (
        """SELECT COUNT(DISTINCT order_id) FROM order_status_history
           WHERE to_status IN ('PARTIALLY_SHIPPED', 'PARTIALLY_DELIVERED')""",
        "SELECT COUNT(DISTINCT order_id) FROM shipments"),
    "cancelled_orders": (
        "SELECT COUNT(*) FROM orders WHERE order_status = 'CANCELLED'",
        "SELECT COUNT(*) FROM orders"),
    "failed_payment_attempts": (
        "SELECT COUNT(*) FROM customer_payments WHERE payment_status = 'FAILED'",
        "SELECT COUNT(*) FROM customer_payments WHERE payment_status <> 'INITIATED'"),
    "partial_payments": (
        """SELECT COUNT(*) FROM (SELECT x.invoice_id FROM customer_payment_allocations x
                                 WHERE x.invoice_id IS NOT NULL AND x.customer_payment_id IS NOT NULL
                                   AND x.allocation_status = 'ACTIVE'
                                 GROUP BY x.invoice_id HAVING COUNT(DISTINCT x.customer_payment_id) > 1) p""",
        """SELECT COUNT(DISTINCT x.invoice_id) FROM customer_payment_allocations x
           WHERE x.invoice_id IS NOT NULL AND x.customer_payment_id IS NOT NULL AND x.allocation_status = 'ACTIVE'"""),
    "overdue_invoices": (
        """SELECT COUNT(*) FROM accounts_receivable a WHERE a.item_type = 'INVOICE'
             AND a.item_status IN ('OPEN', 'PARTIALLY_SETTLED')
             AND a.due_date < (SELECT DATE(MAX(committed_through)) FROM generation_runs)""",
        """SELECT COUNT(*) FROM accounts_receivable a WHERE a.item_type = 'INVOICE'
             AND a.item_status IN ('OPEN', 'PARTIALLY_SETTLED')"""),
    "partial_customer_returns": (
        """SELECT COUNT(DISTINCT r.sales_return_id) FROM sales_returns r
           JOIN sales_return_line_items rl ON rl.sales_return_id = r.sales_return_id
           JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
           WHERE r.return_status <> 'REJECTED' AND rl.quantity_requested < il.quantity""",
        "SELECT COUNT(*) FROM sales_returns WHERE return_status <> 'REJECTED'"),
    "full_customer_returns": (
        """SELECT COUNT(*) FROM sales_returns r WHERE r.return_status <> 'REJECTED'
             AND NOT EXISTS (SELECT 1 FROM sales_return_line_items rl
                             JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
                             WHERE rl.sales_return_id = r.sales_return_id AND rl.quantity_requested < il.quantity)""",
        "SELECT COUNT(*) FROM sales_returns WHERE return_status <> 'REJECTED'"),
    "refunds_pending_approval": (
        "SELECT COUNT(*) FROM refunds WHERE refund_status = 'PENDING_APPROVAL'",
        "SELECT COUNT(*) FROM refunds"),
    "refunds_completed": (
        "SELECT COUNT(*) FROM refunds WHERE refund_status = 'COMPLETED'",
        "SELECT COUNT(*) FROM refunds"),
    "returns_passing_inspection": (
        "SELECT COUNT(*) FROM return_inspections WHERE inspection_result = 'PASSED'",
        "SELECT COUNT(*) FROM return_inspections WHERE inspection_result <> 'FURTHER_INSPECTION'"),
    "defective_goods": (
        "SELECT COUNT(*) FROM return_inspections WHERE inspection_result = 'DEFECTIVE'",
        "SELECT COUNT(*) FROM return_inspections WHERE inspection_result <> 'FURTHER_INSPECTION'"),
    "supplier_quotations_rejected": (
        "SELECT COUNT(*) FROM quotation_headers WHERE quotation_status = 'REJECTED'",
        "SELECT COUNT(*) FROM quotation_headers"),
    "purchase_orders_partially_received": (
        """SELECT COUNT(DISTINCT purchase_order_id) FROM purchase_order_status_history
           WHERE to_status = 'PARTIALLY_RECEIVED'""",
        "SELECT COUNT(DISTINCT purchase_order_id) FROM goods_receipts"),
    "purchase_orders_with_rejected_goods": (
        """SELECT COUNT(DISTINCT g.purchase_order_id) FROM goods_receipts g
           JOIN goods_receipt_line_items l ON l.goods_receipt_id = g.goods_receipt_id
           JOIN goods_inspections i ON i.goods_receipt_line_item_id = l.goods_receipt_line_item_id
           WHERE i.quantity_rejected > 0""",
        "SELECT COUNT(DISTINCT purchase_order_id) FROM goods_receipts"),
    "delayed_supplier_deliveries": (
        """SELECT COUNT(*) FROM (SELECT h.purchase_order_id FROM purchase_order_headers h
                                 JOIN goods_receipts g ON g.purchase_order_id = h.purchase_order_id
                                 GROUP BY h.purchase_order_id, h.expected_delivery_date
                                 HAVING MIN(g.receipt_date) > h.expected_delivery_date) d""",
        "SELECT COUNT(DISTINCT purchase_order_id) FROM goods_receipts"),
    "stock_shortages": (
        """SELECT COUNT(DISTINCT order_id) FROM order_status_history WHERE to_status = 'BACKORDERED'""",
        "SELECT COUNT(*) FROM orders"),
    "inventory_adjustments": (
        """SELECT COUNT(*) FROM stock_adjustment_line_items l JOIN stock_adjustments a
             ON a.stock_adjustment_id = l.stock_adjustment_id WHERE a.adjustment_type <> 'OPENING_STOCK'""",
        """SELECT GREATEST(1, PERIOD_DIFF(DATE_FORMAT(MAX(adjustment_date), '%Y%m'),
                                         DATE_FORMAT(MIN(adjustment_date), '%Y%m')) + 1) FROM stock_adjustments"""),
    "supplier_payment_delays": (
        """SELECT COUNT(*) FROM accounts_payable a WHERE a.item_type = 'SUPPLIER_INVOICE' AND a.item_status = 'SETTLED'
             AND DATE(a.settled_at) > a.due_date""",
        "SELECT COUNT(*) FROM accounts_payable WHERE item_type = 'SUPPLIER_INVOICE' AND item_status = 'SETTLED'"),
    "recurring_and_irregular_expenses": (
        "SELECT COUNT(*) FROM expenses WHERE recurrence_key IS NOT NULL AND expense_type <> 'PAYROLL'",
        "SELECT COUNT(*) FROM expenses WHERE expense_type <> 'PAYROLL'"),
}


@dataclass(frozen=True)
class ScenarioRow:
    scenario: str
    numerator: Decimal
    denominator: Decimal
    value: Decimal | None              # share (or count per month for a 'per_month' scenario)
    band: tuple[float, float]
    unit: str
    status: str                        # OK / LOW / HIGH / NO_DATA
    knobs: tuple[str, ...]


def measure(conn: Connection, scenarios: dict[str, Any]) -> list[ScenarioRow]:
    """Measure every configured scenario on the workspace the connection points at."""
    rows = []
    for name, spec in scenarios.items():
        numerator_sql, denominator_sql = MEASURES[name]
        numerator = Decimal(conn.execute(text(numerator_sql)).scalar() or 0)
        denominator = Decimal(conn.execute(text(denominator_sql)).scalar() or 0)
        value = (numerator / denominator).quantize(Decimal("0.0001")) if denominator else None
        low, high = spec["band"]
        if value is None:
            status = "NO_DATA"
        elif value < Decimal(str(low)):
            status = "LOW"
        elif value > Decimal(str(high)):
            status = "HIGH"
        else:
            status = "OK"
        rows.append(ScenarioRow(name, numerator, denominator, value, (low, high), spec.get("unit", "share"), status,
                                tuple(spec.get("knobs", ()))))
    return rows
