"""Reservations, backorders and cancellations (1C §5 ``inventory_reservations``; 1D §2-3; step 3.8b).

* **Allocation at confirmation.** Each order line reserves sellable stock (sellable − reserved − expired batches,
  "available to promise"): shipped orders from the distribution centre nearest to the shipping address, split over the next
  nearest ones when one can't cover the line; store pickup and counter sales at their store. A line is
  reserved completely or not at all: when total availability is short it waits ``BACKORDERED`` (the order too),
  and the next mornings' **backorder release** reserves it, oldest order first, once receipts have restocked.
  A line still backordered after ``backorder_cancel_days`` is cancelled ``OUT_OF_STOCK`` (the whole order when
  nothing else is left). Reservations change ``inventory.qty_reserved`` only; they are not movements.
* **Customer cancellations.** Whether a customer will cancel, when and why is drawn once per order from a stream
  keyed by the order (D65): ~``customer_cancel_share`` of shipped and pickup orders, ``cancel_delay_hours``
  after placing, mostly ``CUSTOMER_REQUEST``, some ``DUPLICATE_ORDER``. The daily sweep applies the cancellations
  that are due while the order is still before dispatch, releasing its reservations. (Prepaid orders are
  refunded by the returns/refunds step, 3.11.)
* **Backorder demand for replenishment.** Backordered quantity counts against the nearest DC's stock position,
  so the daily stock check orders it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import record_status
from scmgen.generation.engine.order_status import order_status

PRE_DISPATCH = ("PENDING_CREDIT_CHECK", "ON_HOLD", "CONFIRMED", "BACKORDERED")


# ------------------------------------------------------------------------------------------ geography


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Approximate distance (km) between two (latitude, longitude) points; only the order matters."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return 6371 * math.hypot(dx, dy)


def nearest_dcs(day: DayContext, city: str) -> list[int]:
    """Active distribution centres, nearest to the city first."""
    cache = day.world.refs.setdefault("nearest_dcs", {})
    if "dcs" not in cache:
        cache["dcs"] = [(r["warehouse_id"], (float(r["latitude"]), float(r["longitude"]))) for r in day.rows(
            "SELECT warehouse_id, latitude, longitude FROM warehouses WHERE warehouse_type = 'DISTRIBUTION_CENTRE' "
            "AND warehouse_status = 'ACTIVE' ORDER BY warehouse_id")]
    if city not in cache:
        place = next(((float(c["latitude"]), float(c["longitude"])) for c in day.world.seed.cities if c["city"] == city),
                     None)
        dcs = cache["dcs"]
        cache[city] = [w for w, _ in sorted(dcs, key=lambda d: (_distance(place, d[1]), d[0]))] if place else \
            [w for w, _ in dcs]
    return cache[city]


# ------------------------------------------------------------------------------------------ stock


def expired_stock(day: DayContext) -> dict[tuple[int, int], Decimal]:
    """Sellable stock of expired batches per (warehouse, product): it can't be picked (V-IN-04), so it is neither
    available to promise nor part of the stock position, until the expiry sweep writes it off.

    Read in full from the ledger at the first use of a run (D66), then only the batches expiring each new day are
    added: an expired batch never moves in the processes generated so far (no issue picks it). A process that
    moves expired stock (the expiry write-off, a return to origin of an expired batch) must update this view."""
    refs = day.world.refs
    cache = refs.get("expired_stock")
    if cache is not None and cache["through"] >= day.day:
        return cache["quantity"]
    since = "" if cache is None else "AND b.expiry_date > :since"
    found = {(r["warehouse_id"], r["product_id"]): r["expired"] for r in day.rows(f"""
        SELECT t.warehouse_id, t.product_id, SUM(t.quantity) AS expired
        FROM inventory_batches b JOIN inventory_transactions t ON t.inventory_batch_id = b.inventory_batch_id
        WHERE b.expiry_date <= :d {since} AND t.stock_status = 'SELLABLE'
        GROUP BY t.warehouse_id, t.product_id HAVING SUM(t.quantity) > 0""",
        d=day.day, since=cache["through"] if cache else None)}
    if cache is None:
        cache = refs["expired_stock"] = {"through": day.day, "quantity": found}
    else:
        for key, qty in found.items():
            cache["quantity"][key] = cache["quantity"].get(key, Decimal(0)) + qty
        cache["through"] = day.day
    return cache["quantity"]


class Stock:
    """Today's available-to-promise per warehouse × product, the reservations made and the changes to write."""

    def __init__(self, day: DayContext) -> None:
        self.day = day
        self.available: dict[tuple[int, int], Decimal] = {}
        self.loaded: set[int] = set()
        self.delta: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        self.touched: dict[tuple[int, int], datetime] = {}

    def load(self, product_ids: Iterable[int]) -> None:
        """Available to promise = sellable − reserved − sellable stock of expired batches (it can't be picked,
        V-IN-04; the expiry sweep writes it off later)."""
        missing = sorted(set(product_ids) - self.loaded)
        for start in range(0, len(missing), 2000):
            chunk = ", ".join(map(str, missing[start:start + 2000]))
            for row in self.day.rows(f"""
                    SELECT warehouse_id, product_id, qty_sellable - qty_reserved AS available FROM inventory
                    WHERE product_id IN ({chunk})"""):
                self.available[(row["warehouse_id"], row["product_id"])] = row["available"]
            wanted = set(missing[start:start + 2000])
            for key, qty in expired_stock(self.day).items():
                if key[1] in wanted:
                    self.available[key] = self.available.get(key, Decimal(0)) - qty
        self.loaded.update(missing)

    def plan(self, warehouses: list[int], product_id: int, qty: Decimal) -> list[tuple[int, Decimal]] | None:
        """Where the line would be reserved (nearest first, split if needed), or None when stock is short."""
        if sum((max(Decimal(0), self.available.get((w, product_id), Decimal(0))) for w in warehouses),
               Decimal(0)) < qty:
            return None
        parts, left = [], qty
        for warehouse in warehouses:
            free = self.available.get((warehouse, product_id), Decimal(0))
            if free <= 0:
                continue
            take = min(free, left)
            parts.append((warehouse, take))
            left -= take
            if left == 0:
                break
        return parts

    def reserve(self, line_id: int, product_id: int, parts: list[tuple[int, Decimal]], at: datetime) -> None:
        for warehouse, qty in parts:
            self.available[(warehouse, product_id)] -= qty
            self.delta[(warehouse, product_id)] += qty
            self.touched[(warehouse, product_id)] = max(at, self.touched.get((warehouse, product_id), at))
            self.day.writer.insert("inventory_reservations", {
                "inventory_reservation_id": self.day.ids.next("inventory_reservations"),
                "order_line_item_id": line_id, "warehouse_id": warehouse, "reserved_quantity": qty,
                "fulfilled_quantity": Decimal(0), "released_quantity": Decimal(0), "reserved_at": at,
                "closed_at": None, "reservation_status": "ACTIVE", "release_reason": None,
                "created_at": at, "updated_at": at,
            })

    def release_order(self, order_id: int, at: datetime, reason: str) -> None:
        """Release every outstanding reservation of an order (cancellation)."""
        rows = self.day.rows("""
            SELECT r.inventory_reservation_id, r.warehouse_id, l.product_id, r.outstanding_quantity
            FROM inventory_reservations r JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id
            WHERE l.order_id = :o AND r.outstanding_quantity > 0 ORDER BY r.inventory_reservation_id""", o=order_id)
        for row in rows:
            key = (row["warehouse_id"], row["product_id"])
            self.available[key] = self.available.get(key, Decimal(0)) + row["outstanding_quantity"]
            self.delta[key] -= row["outstanding_quantity"]
            self.touched[key] = max(at, self.touched.get(key, at))
        if rows:
            self.day.execute("""
                UPDATE inventory_reservations SET released_quantity = released_quantity + outstanding_quantity,
                       reservation_status = 'RELEASED', closed_at = :t, release_reason = :r, updated_at = :t
                WHERE inventory_reservation_id = :i""",
                [{"t": at, "r": reason, "i": r["inventory_reservation_id"]} for r in rows])

    def write(self) -> None:
        """Apply today's reservation changes to ``inventory.qty_reserved``."""
        changes = [{"w": w, "p": p, "q": q, "t": self.touched[(w, p)]}
                   for (w, p), q in sorted(self.delta.items()) if q != 0]
        if changes:
            self.day.execute("UPDATE inventory SET qty_reserved = qty_reserved + :q, "
                             "updated_at = GREATEST(updated_at, :t) WHERE warehouse_id = :w AND product_id = :p",
                             changes)
        self.delta.clear()


def sources(day: DayContext, fulfilment: str, store_id: int | None, shipping_city: str | None) -> list[int]:
    return [store_id] if fulfilment != "SHIP" else nearest_dcs(day, shipping_city or "")


def allocate_lines(stock: Stock, warehouses: list[int], lines: list[dict[str, Any]], at: datetime) -> list[int]:
    """Reserve each line where possible; returns the ids of the lines that must wait (backordered)."""
    waiting = []
    for line in lines:
        parts = stock.plan(warehouses, line["product_id"], line["quantity"])
        if parts is None:
            waiting.append(line["order_line_item_id"])
        else:
            stock.reserve(line["order_line_item_id"], line["product_id"], parts, at)
    return waiting


# ------------------------------------------------------------------------------------------ open orders of the day


def open_lines(day: DayContext, statuses: tuple[str, ...], line_status: str | None = None) -> dict[int, dict[str, Any]]:
    """Orders in the given statuses with their lines (and what they need for allocation)."""
    in_status = ", ".join(f"'{s}'" for s in statuses)
    line_filter = f"AND l.line_status = '{line_status}'" if line_status else "AND l.line_status <> 'CANCELLED'"
    orders: dict[int, dict[str, Any]] = {}
    for row in day.rows(f"""
            SELECT o.order_id, o.order_status, o.customer_id, o.order_total_estimate, o.fulfilment_type,
                   o.store_warehouse_id, o.updated_at, a.city AS shipping_city, c.credit_limit,
                   l.order_line_item_id, l.product_id, l.quantity_ordered AS quantity, l.updated_at AS line_updated_at
            FROM orders o JOIN order_line_items l ON l.order_id = o.order_id
            JOIN customers c ON c.customer_id = o.customer_id
            LEFT JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
            WHERE o.order_status IN ({in_status}) {line_filter}
            ORDER BY o.order_id, l.line_number"""):
        order = orders.setdefault(row["order_id"], {**{k: row[k] for k in (
            "order_id", "order_status", "customer_id", "order_total_estimate", "fulfilment_type", "store_warehouse_id",
            "updated_at", "shipping_city", "credit_limit")}, "lines": []})
        order["lines"].append({"order_line_item_id": row["order_line_item_id"], "product_id": row["product_id"],
                               "quantity": row["quantity"], "updated_at": row["line_updated_at"]})
    return orders


def mark_backordered(day: DayContext, order_id: int, line_ids: list[int], at: datetime, from_status: str) -> None:
    """Lines that couldn't be reserved wait; so does the order (CONFIRMED → BACKORDERED)."""
    day.execute("UPDATE order_line_items SET line_status = 'BACKORDERED', updated_at = :t WHERE order_line_item_id = :l",
                [{"t": at, "l": line} for line in line_ids])
    day.execute("UPDATE orders SET order_status = 'BACKORDERED', updated_at = :t WHERE order_id = :o",
                {"t": at, "o": order_id})
    record_status(day, "order", order_id, from_status, "BACKORDERED", at, None, "Stock short: waiting for receipts")


def release_backorders(day: DayContext, stock: Stock, backordered: dict[int, dict[str, Any]]) -> None:
    """Every morning (after the receiving dock): reserve backordered lines that stock now covers, oldest order
    first; cancel lines that have waited too long (OUT_OF_STOCK). An order whose other lines have already
    shipped stays PARTIALLY_SHIPPED (or becomes SHIPPED when its last open lines are cancelled)."""
    settings = day.world.config.sales
    at = day.at(9, 30)
    for order_id, order in sorted(backordered.items()):
        status = order["order_status"]
        line_ids = [line["order_line_item_id"] for line in order["lines"]]
        waited = (day.day - min(line["updated_at"] for line in order["lines"]).date()).days
        if waited >= settings.backorder_cancel_days:
            waiting = line_ids                # waited too long: the customer gives up on the missing lines
        else:
            if order["fulfilment_type"] != "SHIP":
                continue                      # store stock is refilled by transfers (inventory step, 3.12)
            warehouses = sources(day, order["fulfilment_type"], order["store_warehouse_id"], order["shipping_city"])
            waiting = allocate_lines(stock, warehouses, order["lines"], at)
            reserved = [line for line in line_ids if line not in waiting]
            if reserved:
                day.execute("UPDATE order_line_items SET line_status = 'OPEN', updated_at = :t "
                            "WHERE order_line_item_id = :l", [{"t": at, "l": line} for line in reserved])
            if waiting or status != "BACKORDERED":
                continue
            day.execute("UPDATE orders SET order_status = 'CONFIRMED', updated_at = :t WHERE order_id = :o",
                        {"t": at, "o": order_id})
            record_status(day, "order", order_id, "BACKORDERED", "CONFIRMED", at, None, "Backorder released: stock reserved")
            continue
        day.execute("UPDATE order_line_items SET line_status = 'CANCELLED', "
                    "quantity_cancelled = quantity_ordered - quantity_shipped, updated_at = :t "
                    "WHERE order_line_item_id = :l", [{"t": at, "l": line} for line in waiting])
        lines = day.rows("""
            SELECT quantity_ordered, quantity_shipped, quantity_delivered, quantity_cancelled, quantity_rto, line_status
            FROM order_line_items WHERE order_id = :o""", o=order_id)
        target = order_status(lines, status)
        if target == "CANCELLED":
            _cancel(day, stock, order_id, status, at, "OUT_OF_STOCK", f"Out of stock for {settings.backorder_cancel_days} days")
            continue
        if target == status and status == "BACKORDERED":
            target = "CONFIRMED"                 # nothing waits any more: the rest is reserved
        if target != status:
            day.execute("UPDATE orders SET order_status = :s, updated_at = :t WHERE order_id = :o",
                        {"s": target, "t": at, "o": order_id})
            record_status(day, "order", order_id, status, target, at, None, "Backordered lines cancelled: out of stock")


# ------------------------------------------------------------------------------------------ cancellations


def cancellation_plan(day: DayContext, order_id: int, order_date, placed_at: datetime) -> tuple[datetime, str] | None:
    """Whether and when the customer cancels the order (drawn once, keyed by the order, D65)."""
    settings = day.world.config.sales
    rng = day.streams.stream(order_date, f"sales.cancel.{order_id}")
    if rng.random() >= settings.customer_cancel_share:
        return None
    reason = "DUPLICATE_ORDER" if rng.random() < settings.duplicate_order_share else "CUSTOMER_REQUEST"
    delay = timedelta(minutes=rng.randint(10, 50)) if reason == "DUPLICATE_ORDER" else \
        timedelta(minutes=rng.randint(settings.cancel_delay_hours[0] * 60, settings.cancel_delay_hours[1] * 60))
    return placed_at + delay, reason


def customer_cancellations(day: DayContext, stock: Stock) -> None:
    """Cancel the orders whose customer cancels today while they are still before dispatch (never before the
    order's, its lines' or its reservations' latest change: a backorder release may have reserved this morning)."""
    settings = day.world.config.sales
    end_of_day = datetime.combine(day.day, time(23, 59, 59))
    earliest = day.day - timedelta(days=settings.cancel_delay_hours[1] // 24 + 2)
    in_status = ", ".join(f"'{s}'" for s in PRE_DISPATCH)
    for order in day.rows(f"""
            SELECT o.order_id, o.order_date, o.order_placed_at, o.order_status,
                   GREATEST(o.updated_at, COALESCE(MAX(r.updated_at), o.updated_at),
                            COALESCE(MAX(l.updated_at), o.updated_at)) AS updated_at
            FROM orders o JOIN order_line_items l ON l.order_id = o.order_id
            LEFT JOIN inventory_reservations r ON r.order_line_item_id = l.order_line_item_id
            WHERE o.order_status IN ({in_status}) AND o.fulfilment_type <> 'COUNTER_SALE' AND o.order_date >= :e
            GROUP BY o.order_id ORDER BY o.order_id""", e=earliest):
        plan = cancellation_plan(day, order["order_id"], order["order_date"], order["order_placed_at"])
        if plan is None:
            continue
        at = max(plan[0], order["updated_at"] + timedelta(minutes=1))
        if at > end_of_day:
            continue
        reason = plan[1]
        text = "Duplicate order placed by mistake" if reason == "DUPLICATE_ORDER" else "Cancelled at the customer's request"
        _cancel(day, stock, order["order_id"], order["order_status"], at, reason, text)


def _cancel(day: DayContext, stock: Stock, order_id: int, from_status: str, at: datetime, reason: str,
            text: str) -> None:
    stock.release_order(order_id, at, text)
    day.execute("UPDATE order_line_items SET quantity_cancelled = quantity_ordered - quantity_shipped, "
                "line_status = 'CANCELLED', updated_at = :t WHERE order_id = :o", {"t": at, "o": order_id})
    day.execute("UPDATE orders SET order_status = 'CANCELLED', hold_reason = NULL, cancelled_at = :t, "
                "cancellation_reason = :r, updated_at = :t WHERE order_id = :o", {"t": at, "r": reason, "o": order_id})
    record_status(day, "order", order_id, from_status, "CANCELLED", at, None, text)


# ------------------------------------------------------------------------------------------ replenishment input


def backorder_demand(day: DayContext) -> dict[tuple[int, int], Decimal]:
    """Backordered quantity of shipped orders per (nearest DC, product): the stock check must order it."""
    demand: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for row in day.rows("""
            SELECT a.city, l.product_id, l.quantity_ordered FROM order_line_items l
            JOIN orders o ON o.order_id = l.order_id JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
            WHERE l.line_status = 'BACKORDERED' AND o.fulfilment_type = 'SHIP' ORDER BY l.order_line_item_id"""):
        dcs = nearest_dcs(day, row["city"])
        if dcs:
            demand[(dcs[0], row["product_id"])] += row["quantity_ordered"]
    return demand
