"""Open orders as fulfilment sees them, and the counter / status updates a dispatch or handover makes.

``OrderBook.load`` reads the orders matching a filter with all their lines and their outstanding reservations
(three queries, driven by the order-status index). Fulfilment then records what it issued (``fulfil``) or had to
drop (``release_rest``), and ``write_orders`` writes reservations, line counters and order statuses once, with a
status-history row for every order whose status changed (1D §3 transitions):

statuses follow ``engine/order_status.py`` (a handover is shipped = delivered = invoiced at once, so ``DELIVERED``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import record_status
from scmgen.generation.engine.order_status import line_status, open_quantity, order_status


@dataclass
class OrderBook:
    day: DayContext
    orders: dict[int, dict[str, Any]]
    lines: dict[int, dict[str, Any]]
    reservations: list[dict[str, Any]]
    touched_orders: dict[int, datetime] = field(default_factory=dict)
    touched_lines: dict[int, datetime] = field(default_factory=dict)
    touched_reservations: dict[int, datetime] = field(default_factory=dict)
    released: dict[tuple[int, int], Decimal] = field(default_factory=dict)

    @classmethod
    def load(cls, day: DayContext, where: str) -> OrderBook:
        orders = {r["order_id"]: r for r in day.rows(f"""
            SELECT o.order_id, o.order_status, o.order_date, o.order_placed_at, o.updated_at, o.customer_id,
                   c.customer_type, o.fulfilment_type, o.store_warehouse_id, o.billing_address_id, o.shipping_address_id,
                   a.city AS shipping_city, a.state_id AS shipping_state_id, o.payment_term_id, t.term_code,
                   o.shipping_charge_amount, o.sales_employee_id
            FROM orders o JOIN customers c ON c.customer_id = o.customer_id
            JOIN payment_terms t ON t.payment_term_id = o.payment_term_id
            LEFT JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
            WHERE {where} ORDER BY o.order_id""")}
        if not orders:
            return cls(day, {}, {}, [])
        in_orders = ", ".join(map(str, orders))
        lines = {r["order_line_item_id"]: r for r in day.rows(f"""
            SELECT order_line_item_id, order_id, line_number, product_id, quantity_ordered, quantity_shipped,
                   quantity_delivered, quantity_invoiced, quantity_cancelled, quantity_rto, unit_price, inclusive_unit_price,
                   discount_percent, line_status
            FROM order_line_items WHERE order_id IN ({in_orders}) ORDER BY order_id, line_number""")}
        reservations = day.rows(f"""
            SELECT r.inventory_reservation_id, r.order_line_item_id, r.warehouse_id, r.reserved_quantity,
                   r.fulfilled_quantity, r.released_quantity, r.outstanding_quantity, r.reserved_at,
                   r.reservation_status, r.release_reason, l.order_id, l.product_id
            FROM inventory_reservations r JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id
            WHERE l.order_id IN ({in_orders}) AND r.outstanding_quantity > 0
            ORDER BY l.order_id, r.warehouse_id, l.line_number""")
        return cls(day, orders, lines, reservations)

    def reservations_of(self, order_id: int) -> list[dict[str, Any]]:
        return [r for r in self.reservations if r["order_id"] == order_id]

    def fulfil(self, reservation: dict[str, Any], qty: Decimal, at: datetime, handover: bool = False) -> None:
        """``qty`` of the reservation was issued at ``at`` (and handed over, for store sales)."""
        reservation["fulfilled_quantity"] += qty
        reservation["outstanding_quantity"] -= qty
        line = self.lines[reservation["order_line_item_id"]]
        line["quantity_shipped"] += qty
        line["quantity_invoiced"] += qty
        if handover:
            line["quantity_delivered"] += qty
        self._touch(reservation, at)

    def release_rest(self, reservation: dict[str, Any], at: datetime, reason: str) -> None:
        """The rest of a reservation can't be handed over (expired batches): released, the line quantity cancelled."""
        rest = reservation["outstanding_quantity"]
        if rest <= 0:
            return
        reservation["released_quantity"] += rest
        reservation["outstanding_quantity"] = Decimal(0)
        reservation["release_reason"] = reason
        self.lines[reservation["order_line_item_id"]]["quantity_cancelled"] += rest
        key = (reservation["warehouse_id"], reservation["product_id"])
        self.released[key] = self.released.get(key, Decimal(0)) + rest
        self._touch(reservation, at)

    def touch_line(self, line_id: int, at: datetime) -> None:
        """A line changed outside a reservation (delivered, returned to origin)."""
        order_id = self.lines[line_id]["order_id"]
        for touched, key in ((self.touched_lines, line_id), (self.touched_orders, order_id)):
            touched[key] = max(at, touched.get(key, at))

    def _touch(self, reservation: dict[str, Any], at: datetime) -> None:
        for touched, key in ((self.touched_reservations, reservation["inventory_reservation_id"]),
                             (self.touched_lines, reservation["order_line_item_id"]),
                             (self.touched_orders, reservation["order_id"])):
            touched[key] = max(at, touched.get(key, at))

    def completes(self, order_id: int) -> bool:
        """After what was issued so far, is every open line of the order fully invoiced?"""
        return all(line["quantity_invoiced"] == open_quantity(line)
                   for line in self.lines.values() if line["order_id"] == order_id)

    # ------------------------------------------------------------------------------------------ writing

    def write_orders(self, mode: str, reason: str | None = None) -> None:
        """``mode``: SHIPPED (dispatch), DELIVERED (handover) or CARRIER (deliveries and returns, with ``reason``)."""
        day = self.day
        for reservation in self.reservations:
            at = self.touched_reservations.get(reservation["inventory_reservation_id"])
            if at is None:
                continue
            done = reservation["outstanding_quantity"] == 0
            status = ("RELEASED" if reservation["fulfilled_quantity"] == 0 else "FULFILLED") if done \
                else "PARTIALLY_FULFILLED"
            day.writer.update("inventory_reservations", "inventory_reservation_id",
                              reservation["inventory_reservation_id"], {
                                  "fulfilled_quantity": reservation["fulfilled_quantity"],
                                  "released_quantity": reservation["released_quantity"],
                                  "release_reason": reservation["release_reason"], "reservation_status": status,
                                  "closed_at": at if done else None, "updated_at": at})
        for line_id, at in sorted(self.touched_lines.items()):
            line = self.lines[line_id]
            line["line_status"] = line_status(line)
            day.writer.update("order_line_items", "order_line_item_id", line_id, {
                "quantity_shipped": line["quantity_shipped"], "quantity_delivered": line["quantity_delivered"],
                "quantity_invoiced": line["quantity_invoiced"], "quantity_cancelled": line["quantity_cancelled"],
                "quantity_rto": line["quantity_rto"], "line_status": line["line_status"], "updated_at": at})
        for order_id, at in sorted(self.touched_orders.items()):
            order = self.orders[order_id]
            # Never before the order's last recorded change (a same-day delivery of one parcel can come before
            # the dispatch of the order's last parcel; the status follows what is known).
            at = max(at, order["updated_at"])
            status = order_status((l for l in self.lines.values() if l["order_id"] == order_id), order["order_status"])
            if status != order["order_status"]:
                cancelled = status == "CANCELLED"
                day.writer.update("orders", "order_id", order_id, {
                    "order_status": status, "cancelled_at": at if cancelled else None,
                    "cancellation_reason": "OUT_OF_STOCK" if cancelled else None, "updated_at": at})
                record_status(day, "order", order_id, order["order_status"], status, at,
                              order["sales_employee_id"] if mode == "DELIVERED" else None,
                              "Reserved stock expired" if cancelled else reason if reason else
                              "Handed over to the customer" if mode == "DELIVERED" else
                              {"SHIPPED": "Dispatched", "PARTIALLY_SHIPPED": "Partly dispatched"}.get(status, "Dispatched"))
                order["order_status"] = status
                order["updated_at"] = at
            else:
                order["updated_at"] = at
                day.writer.update("orders", "order_id", order_id, {"updated_at": at})
        if self.released:
            day.execute("UPDATE inventory SET qty_reserved = qty_reserved - :q, updated_at = GREATEST(updated_at, :t) "
                        "WHERE warehouse_id = :w AND product_id = :p",
                        [{"q": q, "w": w, "p": p, "t": max(self.touched_orders.values())}
                         for (w, p), q in sorted(self.released.items())])
        self.touched_orders.clear()
        self.touched_lines.clear()
        self.touched_reservations.clear()
        self.released.clear()
