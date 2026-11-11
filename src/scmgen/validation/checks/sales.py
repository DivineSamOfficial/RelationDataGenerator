"""Sales rules (1D §9: V-SO-*, V-PY-*). Each check returns the violating rows.

Step 3.8a implements the order side: V-SO-01 (order amounts), V-SO-02, V-SO-05 (statuses up to dispatch), V-SO-06
and V-PY-02 (prepaid part). Step 3.9a adds invoice amounts to V-SO-01 and the shipped / handed-over states to
V-SO-05 (V-SO-03 / V-SO-04: ``fulfilment.py``); the COD part of V-PY-02 comes with deliveries (3.9b).
"""

from __future__ import annotations

from typing import Any

from scmgen.validation.checks.procurement import status_chain_problems
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

OVERRIDE_REASON = "Finance override"

# Allowed order transitions (1D §3). COD orders pass an automatic eligibility check; counter sales start CONFIRMED.
ORDER_TRANSITIONS: set[tuple[str | None, str]] = {
    (None, "PENDING_PAYMENT"), (None, "PENDING_CREDIT_CHECK"), (None, "CONFIRMED"),
    ("PENDING_PAYMENT", "CONFIRMED"), ("PENDING_PAYMENT", "CANCELLED"),
    ("PENDING_CREDIT_CHECK", "CONFIRMED"), ("PENDING_CREDIT_CHECK", "ON_HOLD"), ("PENDING_CREDIT_CHECK", "CANCELLED"),
    ("ON_HOLD", "CONFIRMED"), ("ON_HOLD", "CANCELLED"),
    ("CONFIRMED", "BACKORDERED"), ("BACKORDERED", "CONFIRMED"),
    ("CONFIRMED", "PARTIALLY_SHIPPED"), ("CONFIRMED", "SHIPPED"), ("BACKORDERED", "PARTIALLY_SHIPPED"),
    ("PARTIALLY_SHIPPED", "SHIPPED"), ("SHIPPED", "PARTIALLY_DELIVERED"), ("SHIPPED", "DELIVERED"),
    ("PARTIALLY_DELIVERED", "DELIVERED"), ("DELIVERED", "CLOSED"),
    ("CONFIRMED", "DELIVERED"),                         # store handover: counter sale or pickup (3.9a)
    ("CONFIRMED", "CANCELLED"), ("BACKORDERED", "CANCELLED"),
}
PRE_DISPATCH = ("PENDING_PAYMENT", "PENDING_CREDIT_CHECK", "ON_HOLD", "CONFIRMED", "BACKORDERED")


@check("V-SO-01")
def order_amounts(ctx: ValidationContext) -> Rows:
    """Order header = Σ lines; estimate = items + GST + shipping + its GST at the highest line rate, rounded
    to the nearest rupee; line GST rate = the product's rate on the order date; GST-inclusive prices exactly for B2C
    orders (D79); selling price incl. GST ≤ MRP; shipping charged only on shipped orders (D42)."""
    found = [{**r, "problem": "header ≠ Σ lines"} for r in ctx.rows("""
        SELECT o.order_id, o.items_taxable_amount, s.taxable, o.items_tax_amount, s.tax FROM orders o
        LEFT JOIN (SELECT order_id, SUM(taxable_amount) AS taxable, SUM(tax_amount) AS tax FROM order_line_items
                   GROUP BY order_id) s ON s.order_id = o.order_id
        WHERE s.order_id IS NULL OR o.items_taxable_amount <> s.taxable OR o.items_tax_amount <> s.tax""")]
    found += [{**r, "problem": "estimate ≠ items + GST + shipping + shipping GST, rounded"} for r in ctx.rows("""
        SELECT o.order_id, o.order_total_estimate, s.top_rate FROM orders o
        JOIN (SELECT order_id, MAX(gst_rate) AS top_rate FROM order_line_items GROUP BY order_id) s ON s.order_id = o.order_id
        WHERE o.order_total_estimate <> ROUND(o.items_taxable_amount + o.items_tax_amount + o.shipping_charge_amount
                                             + ROUND(o.shipping_charge_amount * s.top_rate / 100, 2), 0)""")]
    found += [{**r, "problem": "GST rate ≠ product rate on the order date"} for r in ctx.rows("""
        SELECT o.order_id, l.line_number, l.gst_rate, t.gst_rate AS product_rate FROM orders o
        JOIN order_line_items l ON l.order_id = o.order_id
        LEFT JOIN product_tax_rates t ON t.product_id = l.product_id AND t.effective_from <= o.order_date
             AND (t.effective_to IS NULL OR t.effective_to >= o.order_date)
        WHERE t.gst_rate IS NULL OR l.gst_rate <> t.gst_rate""")]
    found += [{**r, "problem": "GST-inclusive pricing ≠ B2C customer"} for r in ctx.rows("""
        SELECT o.order_id, o.prices_include_tax, c.customer_type, l.line_number FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id JOIN order_line_items l ON l.order_id = o.order_id
        WHERE o.prices_include_tax <> (c.customer_type = 'B2C')
           OR o.prices_include_tax <> (l.inclusive_unit_price IS NOT NULL)""")]
    found += [{**r, "problem": "selling price incl. GST above MRP"} for r in ctx.rows("""
        SELECT o.order_id, l.line_number, l.inclusive_unit_price, l.unit_price, l.discount_percent, p.mrp
        FROM orders o JOIN order_line_items l ON l.order_id = o.order_id JOIN products p ON p.product_id = l.product_id
        WHERE COALESCE(l.inclusive_unit_price, l.unit_price * (1 + l.gst_rate / 100)) * (1 - l.discount_percent / 100)
              > p.mrp""")]
    found += [{**r, "problem": "shipping charge on an order that isn't shipped"} for r in ctx.rows(
        "SELECT order_id, fulfilment_type, shipping_charge_amount FROM orders "
        "WHERE fulfilment_type <> 'SHIP' AND shipping_charge_amount <> 0")]
    found += [{**r, "problem": "invoice header ≠ Σ lines + round-off"} for r in ctx.rows("""
        SELECT i.invoice_id, i.taxable_amount, s.taxable, i.cgst_amount, s.cgst, i.igst_amount, s.igst FROM invoices i
        LEFT JOIN (SELECT invoice_id, SUM(taxable_amount) AS taxable, SUM(cgst_amount) AS cgst,
                          SUM(sgst_amount) AS sgst, SUM(igst_amount) AS igst FROM invoice_line_items GROUP BY invoice_id) s
               ON s.invoice_id = i.invoice_id
        WHERE s.invoice_id IS NULL OR i.taxable_amount <> s.taxable OR i.cgst_amount <> s.cgst
           OR i.sgst_amount <> s.sgst OR i.igst_amount <> s.igst
           OR i.total_amount <> s.taxable + s.cgst + s.sgst + s.igst + i.round_off_amount""")]
    found += [{**r, "problem": "tax split doesn't match the place of supply"} for r in ctx.rows("""
        SELECT i.invoice_id, i.is_inter_state, i.place_of_supply_state_id, g.state_id AS gstin_state, l.line_number
        FROM invoices i JOIN company_gst_registrations g ON g.company_gst_registration_id = i.company_gst_registration_id
        JOIN invoice_line_items l ON l.invoice_id = i.invoice_id
        WHERE i.is_inter_state <> (i.place_of_supply_state_id <> g.state_id)
           OR (i.is_inter_state AND (l.cgst_amount <> 0 OR l.sgst_amount <> 0))
           OR (NOT i.is_inter_state AND l.igst_amount <> 0)""")]
    return found


@check("V-SO-02")
def order_attribution(ctx: ValidationContext) -> Rows:
    """A1 / D10: campaign on the order's marketing channel, running on the order date, for the customer type and
    the products' business unit; sales channel allowed for the customer type; COUNTER_SALE ⇔ the store channel;
    the store is a RETAIL_STORE; addresses of the right kind and live when the order was placed; no order
    before the customer's acquisition date."""
    found = [{**r, "problem": "campaign doesn't fit the order"} for r in ctx.rows("""
        SELECT o.order_id, o.order_date, c.marketing_campaign_id, c.campaign_status FROM orders o
        JOIN marketing_campaigns c ON c.marketing_campaign_id = o.marketing_campaign_id
        JOIN customers k ON k.customer_id = o.customer_id
        WHERE c.marketing_channel_id <> o.marketing_channel_id OR o.order_date NOT BETWEEN c.start_date AND c.end_date
           OR c.campaign_status = 'CANCELLED' OR c.target_customer_type NOT IN (k.customer_type, 'ALL')
           OR EXISTS (SELECT 1 FROM order_line_items l JOIN products p ON p.product_id = l.product_id
                      WHERE l.order_id = o.order_id AND p.business_unit_id <> c.business_unit_id)""")]
    found += [{**r, "problem": "sales channel not allowed for the customer type"} for r in ctx.rows("""
        SELECT o.order_id, s.channel_code, k.customer_type FROM orders o
        JOIN sales_channels s ON s.sales_channel_id = o.sales_channel_id JOIN customers k ON k.customer_id = o.customer_id
        WHERE s.allowed_customer_type NOT IN (k.customer_type, 'BOTH')""")]
    found += [{**r, "problem": "counter sale ⇔ store channel, store must be a RETAIL_STORE"} for r in ctx.rows("""
        SELECT o.order_id, o.fulfilment_type, s.channel_code, w.warehouse_type FROM orders o
        JOIN sales_channels s ON s.sales_channel_id = o.sales_channel_id
        LEFT JOIN warehouses w ON w.warehouse_id = o.store_warehouse_id
        WHERE (o.fulfilment_type = 'COUNTER_SALE') <> (NOT s.requires_shipment)
           OR (o.store_warehouse_id IS NOT NULL AND w.warehouse_type <> 'RETAIL_STORE')""")]
    found += [{**r, "problem": "address of the wrong kind or not live at the order time"} for r in ctx.rows("""
        SELECT o.order_id, a.customer_address_id, a.valid_from, a.deleted_at FROM orders o
        JOIN customer_addresses a ON a.customer_address_id = o.billing_address_id
        WHERE a.valid_from > o.order_date OR (a.deleted_at IS NOT NULL AND a.deleted_at <= o.order_placed_at)
           OR NOT a.is_billing_address
        UNION ALL
        SELECT o.order_id, a.customer_address_id, a.valid_from, a.deleted_at FROM orders o
        JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
        WHERE a.valid_from > o.order_date OR (a.deleted_at IS NOT NULL AND a.deleted_at <= o.order_placed_at)
           OR NOT a.is_shipping_address""")]
    found += [{**r, "problem": "order before the customer's acquisition date"} for r in ctx.rows("""
        SELECT o.order_id, o.order_date, k.acquisition_date FROM orders o JOIN customers k ON k.customer_id = o.customer_id
        WHERE o.order_date < k.acquisition_date""")]
    return found


@check("V-SO-05")
def order_statuses(ctx: ValidationContext) -> Rows:
    """Status histories are continuous chains of allowed transitions ending at the order's status; a cancelled
    order has every line cancelled; an order before dispatch has nothing shipped and only OPEN / BACKORDERED /
    CANCELLED lines; BACKORDERED ⇔ some line BACKORDERED (before dispatch); a CANCELLED line has all of its
    quantity cancelled; a line's status fits its quantities (PARTIALLY_SHIPPED, SHIPPED, DELIVERED); a SHIPPED
    order has every open line shipped, a PARTIALLY_SHIPPED one some but not all, a DELIVERED one every open line
    delivered."""
    found = status_chain_problems(ctx, "order", "orders", "order_id", "order_status", ORDER_TRANSITIONS)
    pre = ", ".join(f"'{s}'" for s in PRE_DISPATCH)
    found += [{**r, "problem": "line state doesn't fit the order status"} for r in ctx.rows(f"""
        SELECT o.order_id, o.order_status, l.line_number, l.line_status, l.quantity_ordered, l.quantity_shipped,
               l.quantity_cancelled FROM orders o JOIN order_line_items l ON l.order_id = o.order_id
        WHERE (o.order_status = 'CANCELLED' AND (l.line_status <> 'CANCELLED'
                                                OR l.quantity_cancelled <> l.quantity_ordered - l.quantity_shipped))
           OR (o.order_status IN ({pre}) AND (l.quantity_shipped <> 0 OR l.line_status NOT IN ('OPEN', 'BACKORDERED', 'CANCELLED')))
           OR (l.line_status = 'CANCELLED' AND l.quantity_cancelled <> l.quantity_ordered)
           OR (l.line_status IN ('OPEN', 'BACKORDERED') AND l.quantity_shipped <> 0)
           OR (l.line_status = 'PARTIALLY_SHIPPED'
               AND NOT (l.quantity_shipped > 0 AND l.quantity_shipped < l.quantity_ordered - l.quantity_cancelled))
           OR (l.line_status = 'SHIPPED' AND l.quantity_shipped <> l.quantity_ordered - l.quantity_cancelled)
           OR (l.line_status = 'DELIVERED' AND (l.quantity_shipped <> l.quantity_ordered - l.quantity_cancelled
                                                OR l.quantity_delivered + l.quantity_rto <> l.quantity_shipped))""")]
    found += [{**r, "problem": "BACKORDERED order ⇔ a BACKORDERED line"} for r in ctx.rows(f"""
        SELECT o.order_id, o.order_status, SUM(l.line_status = 'BACKORDERED') AS backordered_lines FROM orders o
        JOIN order_line_items l ON l.order_id = o.order_id WHERE o.order_status IN ({pre})
        GROUP BY o.order_id, o.order_status
        HAVING (o.order_status = 'BACKORDERED') <> (SUM(l.line_status = 'BACKORDERED') > 0)""")]
    found += [{**r, "problem": "open order with every line cancelled"} for r in ctx.rows(f"""
        SELECT o.order_id, o.order_status FROM orders o JOIN order_line_items l ON l.order_id = o.order_id
        WHERE o.order_status IN ({pre}) GROUP BY o.order_id, o.order_status
        HAVING SUM(l.line_status <> 'CANCELLED') = 0""")]
    found += [{**r, "problem": "order status doesn't fit what its lines shipped or delivered"} for r in ctx.rows("""
        SELECT o.order_id, o.order_status, SUM(l.line_status <> 'CANCELLED') AS open_lines,
               SUM(l.quantity_shipped > 0) AS shipped_lines,
               SUM(l.line_status <> 'CANCELLED' AND l.quantity_shipped < l.quantity_ordered - l.quantity_cancelled)
                   AS unshipped_lines,
               SUM(l.line_status NOT IN ('CANCELLED', 'DELIVERED')) AS undelivered_lines
        FROM orders o JOIN order_line_items l ON l.order_id = o.order_id
        WHERE o.order_status IN ('PARTIALLY_SHIPPED', 'SHIPPED', 'DELIVERED') GROUP BY o.order_id, o.order_status
        HAVING open_lines = 0
            OR (o.order_status = 'SHIPPED' AND unshipped_lines > 0)
            OR (o.order_status = 'PARTIALLY_SHIPPED' AND (unshipped_lines = 0 OR shipped_lines = 0))
            OR (o.order_status = 'DELIVERED' AND undelivered_lines > 0)""")]
    return found


@check("V-SO-06")
def credit_holds(ctx: ValidationContext) -> Rows:
    """D47: a B2B order on credit is confirmed only while open AR (as of that moment) + the order's value is
    within the credit limit, unless Finance overrode the hold (by an approver whose limit covers the order).
    Open AR at a moment = Σ AR items recorded by then (allocations only move amounts between the customer's
    own items); a reversed item counts until it was reversed. Credit limits are kept without history (like
    prices, D37), so only confirmations made since the customer row last changed are judged."""
    found = [{**r, "problem": "confirmed above the credit limit"} for r in ctx.rows(f"""
        SELECT o.order_id, h.changed_at, k.credit_limit, o.order_total_estimate,
               COALESCE((SELECT SUM(a.original_amount) FROM accounts_receivable a
                         WHERE a.customer_id = o.customer_id AND a.created_at <= h.changed_at
                           AND (a.item_status <> 'REVERSED' OR a.updated_at > h.changed_at)), 0) AS open_ar
        FROM orders o JOIN customers k ON k.customer_id = o.customer_id
        JOIN order_status_history h ON h.order_id = o.order_id AND h.to_status = 'CONFIRMED'
             AND h.from_status IN ('PENDING_CREDIT_CHECK', 'ON_HOLD')
        WHERE k.customer_type = 'B2B' AND k.credit_limit IS NOT NULL AND h.changed_at >= k.updated_at
          AND COALESCE(h.change_reason, '') <> '{OVERRIDE_REASON}'
        HAVING open_ar + o.order_total_estimate > k.credit_limit""")]
    found += [{**r, "problem": "override by someone whose approval limit doesn't cover the order"} for r in ctx.rows(f"""
        SELECT o.order_id, h.changed_by_employee_id, e.approval_limit, o.order_total_estimate FROM orders o
        JOIN order_status_history h ON h.order_id = o.order_id AND h.change_reason = '{OVERRIDE_REASON}'
        LEFT JOIN employees e ON e.employee_id = h.changed_by_employee_id
        WHERE h.from_status <> 'ON_HOLD' OR h.to_status <> 'CONFIRMED' OR e.employee_id IS NULL
           OR e.approval_limit < o.order_total_estimate""")]
    return found


@check("V-PY-02")
def prepaid_orders_paid_first(ctx: ValidationContext) -> Rows:
    """Prepaid orders are confirmed only after a SUCCESS prepayment ≥ the order estimate (D5); at most one
    successful prepayment per order; orders cancelled for failed payment have none. (COD part: step 3.9.)"""
    found = [{**r, "problem": "prepaid order confirmed without a prior SUCCESS payment ≥ estimate"} for r in ctx.rows("""
        SELECT o.order_id, h.changed_at, o.order_total_estimate FROM orders o
        JOIN payment_terms t ON t.payment_term_id = o.payment_term_id
        JOIN order_status_history h ON h.order_id = o.order_id AND h.to_status = 'CONFIRMED'
        WHERE t.is_prepaid AND NOT EXISTS (
            SELECT 1 FROM customer_payments p WHERE p.order_id = o.order_id AND p.payment_type = 'ORDER_PREPAYMENT'
              AND p.payment_status IN ('SUCCESS', 'REVERSED') AND p.amount >= o.order_total_estimate
              AND p.completed_at <= h.changed_at)""")]
    found += [{**r, "problem": "more than one successful prepayment, or one on a PAYMENT_FAILED order"} for r in ctx.rows("""
        SELECT o.order_id, o.cancellation_reason, COUNT(*) AS successes FROM orders o
        JOIN customer_payments p ON p.order_id = o.order_id AND p.payment_type = 'ORDER_PREPAYMENT'
             AND p.payment_status IN ('SUCCESS', 'REVERSED')
        GROUP BY o.order_id, o.cancellation_reason
        HAVING COUNT(*) > 1 OR o.cancellation_reason = 'PAYMENT_FAILED'""")]
    return found
