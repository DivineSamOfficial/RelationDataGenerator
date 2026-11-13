"""Demand-driven stock targets (1C §3.0, §5; step 3.14c-fix).

The master-data reorder policy (``products.reorder_level`` / ``reorder_quantity``, copied to ``inventory``) is a
*value-based* default (about ₹50,000 at MRP), not a forecast. Replenishing to it bought ~10× what sold for cheap
fast-moving consumer goods, and the excess expired (found when the expiry sweep, 3.12b, started writing it off).
Warehouses now plan like a replenishment planner does:

* **Demand rate** per warehouse × product = units sold and sent to its stores (not rebalancing) over the last
  ``procurement.demand_window_days``, per day of that window (also at the start of the history). Read from the ledger once per day.
* **Reorder point** = rate × ``reorder_cover_days``; **order up to** rate × ``target_cover_days`` — for a product
  with a shelf life at most ``perishable_cover_days`` and a third of its shelf life, so stock sells before it
  expires. Stores use their own cover days (``inventory.store_*_cover_days``).
* A product nobody bought recently isn't reordered (its backorders still are: they count against the position).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from scmgen.generation.engine.day import DayContext

ZERO = Decimal(0)


@dataclass(frozen=True)
class Target:
    rate: Decimal              # units per day
    reorder_point: Decimal
    order_up_to: Decimal


def daily_rates(day: DayContext, warehouse_ids: list[int]) -> dict[tuple[int, int], Decimal]:
    """Units per day that left each warehouse × product over the demand window (sales and transfers out)."""
    key = ("demand_rates", tuple(sorted(warehouse_ids)))
    if key in day.notes:
        return day.notes[key]
    settings = day.world.config.procurement
    elapsed = (day.day - day.world.config.start_date).days
    window = min(settings.demand_window_days, max(elapsed, 0))
    rates: dict[tuple[int, int], Decimal] = {}
    # Always per day of the full window: early in the history (a few days of sales) one order would otherwise look
    # like a high rate; dividing by the full window underestimates instead, while the opening stock still covers.
    if window >= 1 and warehouse_ids:
        in_w = ", ".join(map(str, sorted(warehouse_ids)))
        # Sales and the stock a DC sends to its stores are demand; rebalancing between DCs is not (counting it made
        # the donor reorder what it had just given away, and stock went back and forth).
        for row in day.rows(f"""
                SELECT warehouse_id, product_id, -SUM(units) AS units FROM (
                    SELECT warehouse_id, product_id, quantity AS units FROM inventory_transactions
                    WHERE transaction_type = 'SALES_ISSUE' AND transaction_at >= :a AND transaction_at < :b
                      AND warehouse_id IN ({in_w})
                    UNION ALL
                    SELECT t.warehouse_id, t.product_id, t.quantity FROM inventory_transactions t
                    JOIN stock_transfer_line_items l ON l.stock_transfer_line_item_id = t.stock_transfer_line_item_id
                    JOIN stock_transfers s ON s.stock_transfer_id = l.stock_transfer_id
                    WHERE t.transaction_type = 'TRANSFER_OUT' AND s.transfer_type = 'STORE_REPLENISHMENT'
                      AND t.transaction_at >= :a AND t.transaction_at < :b AND t.warehouse_id IN ({in_w})) x
                GROUP BY warehouse_id, product_id HAVING SUM(units) < 0""",
                a=day.at(0) - timedelta(days=window), b=day.at(0)):
            rates[(row["warehouse_id"], row["product_id"])] = Decimal(row["units"]) / settings.demand_window_days
    day.notes[key] = rates
    return rates


def target(rate: Decimal, shelf_life_days: int | None, reorder_days: int, cover_days: int,
           perishable_days: int) -> Target:
    """Reorder point and order-up-to level for a daily rate (whole units, at least one when there is demand)."""
    if shelf_life_days:
        cover_days = max(1, min(cover_days, perishable_days, shelf_life_days // 3))
        reorder_days = min(reorder_days, max(1, cover_days // 2))
    up_to = Decimal(math.ceil(rate * cover_days)) if rate > 0 else ZERO
    point = Decimal(math.ceil(rate * reorder_days)) if rate > 0 else ZERO
    return Target(rate, point, max(up_to, point))


def dc_target(day: DayContext, rate: Decimal, shelf_life_days: int | None) -> Target:
    settings = day.world.config.procurement
    return target(rate, shelf_life_days, settings.reorder_cover_days, settings.target_cover_days,
                  settings.perishable_cover_days)


def store_target(day: DayContext, rate: Decimal, shelf_life_days: int | None) -> Target:
    procurement, inventory = day.world.config.procurement, day.world.config.inventory
    return target(rate, shelf_life_days, inventory.store_reorder_cover_days, inventory.store_target_cover_days,
                  procurement.perishable_cover_days)


def on_order(day: DayContext, warehouse_ids: list[int]) -> dict[tuple[int, int], Decimal]:
    """Stock on its way or being sourced per warehouse × product: open PO lines, open RFQ lines, open transfers in."""
    from scmgen.generation.engine.inventory.transfers import inbound_transfers
    from scmgen.generation.engine.procurement.sourcing import OPEN_PO_STATUSES, OPEN_RFQ_STATUSES
    found: dict[tuple[int, int], Decimal] = {}
    if not warehouse_ids:
        return found
    in_w = ", ".join(map(str, sorted(warehouse_ids)))
    for row in day.rows(f"""
            SELECT h.delivery_warehouse_id AS warehouse_id, l.product_id, SUM(l.quantity_open) AS qty
            FROM purchase_order_line_items l JOIN purchase_order_headers h ON h.purchase_order_id = l.purchase_order_id
            WHERE h.po_status IN ({", ".join(f"'{s}'" for s in OPEN_PO_STATUSES)}) AND h.delivery_warehouse_id IN ({in_w})
            GROUP BY 1, 2
            UNION ALL
            SELECT h.delivery_warehouse_id, l.product_id, SUM(l.requested_quantity)
            FROM rfq_line_items l JOIN rfq_headers h ON h.rfq_id = l.rfq_id
            WHERE h.rfq_status IN ({", ".join(f"'{s}'" for s in OPEN_RFQ_STATUSES)}) AND h.delivery_warehouse_id IN ({in_w})
            GROUP BY 1, 2"""):
        key = (row["warehouse_id"], row["product_id"])
        found[key] = found.get(key, ZERO) + row["qty"]
    for key, qty in inbound_transfers(day, warehouse_ids).items():
        found[key] = found.get(key, ZERO) + qty
    return found
