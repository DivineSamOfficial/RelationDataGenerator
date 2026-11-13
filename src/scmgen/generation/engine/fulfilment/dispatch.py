"""Warehouse dispatch of shipped orders (1D §2, §4; D4, D48; step 3.9a).

On every working day each distribution centre works one wave: the outstanding reservations of shipped orders
reserved before ``dispatch_cutoff_hour`` (later ones wait for the next working day). One **shipment** per order
× warehouse goes through PENDING → PICKING → PICKED → PACKING → PACKED → READY_FOR_PICKUP → PICKED_UP within the
day; PICKED_UP is the dispatch: the carrier collects, the stock is issued (FEFO batches, one shipment line per
batch), the reservation is fulfilled and the tax invoice is issued (``Invoicer.bill``) in the same transaction.
Reserved lines ship even while other lines of the order are backordered (the order goes PARTIALLY_SHIPPED).
Work on a shipment starts once its stock is in (a receipt inspected in the afternoon is picked after it); a
shipment that can't be ready before the carrier's last pickup waits for the next working day.

Carrier and freight: the consignment's weight picks the service (express courier, surface LTL from
``surface_min_kg``, full truckload from ``truckload_min_kg``; nearby consumer parcels may go hyperlocal); COD
parcels only go with carriers that collect cash. Freight = the carrier type's rate card (per shipment, per kg,
per kg per 100 km). The expected delivery date follows the distance bands in ``transit_days``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money
from scmgen.domain.identifiers import eway_bill_number, tracking_number
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import record_status
from scmgen.generation.engine.fulfilment.invoicing import BillLine, Invoicer
from scmgen.generation.engine.fulfilment.orders import OrderBook
from scmgen.generation.engine.fulfilment.stock import Issuer, ledger_row
from scmgen.generation.engine.sales.allocation import _distance
from scmgen.generation.numbering import Registration

SHIPPABLE = ("CONFIRMED", "BACKORDERED", "PARTIALLY_SHIPPED", "PARTIALLY_DELIVERED")
EXPIRED_REASON = "Stock expired before dispatch"
WAREHOUSE_STEPS = ("PICKING", "PICKED", "PACKING", "PACKED", "READY_FOR_PICKUP")
STEP_MINUTES = {"PICKING": (5, 60), "PICKED": (15, 90), "PACKING": (5, 30), "PACKED": (10, 45),
                "READY_FOR_PICKUP": (5, 30)}


def _carriers(day: DayContext) -> list[dict[str, Any]]:
    if "carriers" not in day.world.refs:
        day.world.refs["carriers"] = day.rows(
            "SELECT shipping_carrier_id, carrier_type, supports_cod, tracking_number_prefix FROM shipping_carriers "
            "WHERE is_active ORDER BY shipping_carrier_id")
    return day.world.refs["carriers"]


def _place(day: DayContext, city: str) -> tuple[float, float] | None:
    cities = day.world.refs.setdefault("city_coordinates", {
        c["city"]: (float(c["latitude"]), float(c["longitude"])) for c in day.world.seed.cities})
    return cities.get(city)


def transit_days(day: DayContext, carrier_type: str, km: float) -> int:
    settings = day.world.config.fulfilment
    days = next((d for limit, d in settings.transit_days if km <= limit), settings.transit_days[-1][1])
    return days + (settings.surface_extra_days if carrier_type in ("SURFACE_LTL", "FULL_TRUCKLOAD") else 0)


def expected_delivery(dispatched: date, days: int) -> date:
    """The carrier's promised date: dispatch + transit days, never a Sunday (no deliveries)."""
    expected = dispatched + timedelta(days=days)
    return expected + timedelta(days=1) if expected.weekday() == 6 else expected


def dispatch_step(day: DayContext, invoicer: Invoicer) -> None:
    if not day.is_working_day:
        return
    settings = day.world.config.fulfilment
    cutoff = day.at(settings.dispatch_cutoff_hour)
    book = OrderBook.load(day, f"""
        o.fulfilment_type = 'SHIP' AND o.order_status IN ({", ".join(f"'{s}'" for s in SHIPPABLE)})""")
    waves: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for reservation in book.reservations:
        warehouse = invoicer.warehouses[reservation["warehouse_id"]]
        if reservation["reserved_at"] < cutoff and warehouse["warehouse_type"] == "DISTRIBUTION_CENTRE":
            waves[(reservation["order_id"], reservation["warehouse_id"])].append(reservation)
    if not waves:
        return
    invoicer.load(list(book.orders.values()), {r["product_id"] for group in waves.values() for r in group})
    issuer = Issuer(day, {(w, r["product_id"]) for (_, w), group in waves.items() for r in group})
    crews = _crews(day, sorted({w for _, w in waves}))
    # Plan every shipment's warehouse day first and dispatch them in time order: the moving-average cost of a
    # product moves (by rounding) with each issue, so issues must happen in the order of their timestamps.
    last_pickup = day.at(settings.carrier_pickup_hours[1], 30)
    plans = []
    for (order_id, warehouse_id), reservations in sorted(waves.items()):
        rng = day.rng(f"fulfilment.shipment.{order_id}.{warehouse_id}")
        timeline = _timeline(day, rng, issuer, warehouse_id, reservations)
        if timeline[-1][1] <= last_pickup:          # not ready before the carrier's last pickup: next working day
            plans.append((timeline, order_id, warehouse_id, rng))
    for timeline, order_id, warehouse_id, rng in sorted(plans, key=lambda p: (p[0][-1][1], p[1], p[2])):
        _ship(day, invoicer, issuer, book, crews[warehouse_id], book.orders[order_id], warehouse_id,
              waves[(order_id, warehouse_id)], timeline, rng)
    issuer.write()
    book.write_orders("SHIPPED")


def _crews(day: DayContext, warehouses: list[int]) -> dict[int, dict[str, list[int]]]:
    crews: dict[int, dict[str, list[int]]] = {w: {"STAFF": [], "LEAD": []} for w in warehouses}
    for row in day.rows(f"""
            SELECT employee_id, warehouse_id, job_level FROM employees
            WHERE warehouse_id IN ({", ".join(map(str, warehouses))}) AND employment_status = 'ACTIVE'
              AND hire_date <= :d AND job_level IN ('STAFF', 'LEAD', 'MANAGER') ORDER BY employee_id""", d=day.day):
        crews[row["warehouse_id"]]["STAFF" if row["job_level"] == "STAFF" else "LEAD"].append(row["employee_id"])
    for crew in crews.values():
        crew["STAFF"] = crew["STAFF"] or crew["LEAD"]
    return crews


def _timeline(day: DayContext, rng, issuer: Issuer, warehouse_id: int,
              reservations: list[dict[str, Any]]) -> list[tuple[str, datetime]]:
    """The shipment's warehouse day: PENDING … READY_FOR_PICKUP, then PICKED_UP when the carrier collects. Work
    starts once the order is reserved and its stock is in (today's receipts are inspected in the afternoon)."""
    settings = day.world.config.fulfilment
    arrived = [a for a in (issuer.arrived_at(warehouse_id, r["product_id"]) for r in reservations) if a is not None]
    at = max(day.at(settings.pick_start_hour) - timedelta(minutes=15),
             max(r["reserved_at"] for r in reservations) + timedelta(minutes=5),
             max(arrived, default=day.at(0)) + timedelta(minutes=5))
    moments = [("PENDING", at)]
    for status in WAREHOUSE_STEPS:
        at = at + timedelta(minutes=rng.randint(*STEP_MINUTES[status]), seconds=rng.randint(0, 59))
        moments.append((status, at))
    window = (settings.carrier_pickup_hours[1] - settings.carrier_pickup_hours[0]) * 60
    moments.append(("PICKED_UP", max(at + timedelta(minutes=rng.randint(10, 40)),
                                     day.at(settings.carrier_pickup_hours[0])
                                     + timedelta(minutes=rng.randint(0, window), seconds=rng.randint(0, 59)))))
    return moments


def _ship(day: DayContext, invoicer: Invoicer, issuer: Issuer, book: OrderBook, crew: dict[str, list[int]],
          order: dict[str, Any], warehouse_id: int, reservations: list[dict[str, Any]],
          timeline: list[tuple[str, datetime]], rng) -> None:
    settings = day.world.config.fulfilment
    created, dispatched = timeline[0][1], timeline[-1][1]
    picks, short = [], []
    for reservation in reservations:
        qty = issuer.pickable(warehouse_id, reservation["product_id"], reservation["outstanding_quantity"], day.day)
        if qty > 0:
            picks.append((reservation, qty))
        if qty < reservation["outstanding_quantity"]:
            short.append(reservation)
    if not picks:                               # the reserved stock expired before it could be picked
        for reservation in short:
            book.release_rest(reservation, created, EXPIRED_REASON)
        return
    shipment_id = day.ids.next("shipments")

    # Pick: stock issue per batch, one shipment line each.
    shipment_lines, bill_lines, weight = [], [], Decimal(0)
    for reservation, qty in picks:
        product = invoicer.products[reservation["product_id"]]
        weight += qty * product["weight_kg"]
        for piece in issuer.issue(warehouse_id, reservation["product_id"], qty, dispatched):
            line_id = day.ids.next("shipment_line_items")
            shipment_lines.append((line_id, reservation, piece))
            bill_lines.append(BillLine(book.lines[reservation["order_line_item_id"]], piece.quantity,
                                       -piece.movement.value_change, shipment_line_item_id=line_id))
        book.fulfil(reservation, qty, dispatched)
    for reservation in short:                   # the rest expired meanwhile: released, the quantity cancelled
        book.release_rest(reservation, dispatched, EXPIRED_REASON)

    # Carrier and freight.
    warehouse = invoicer.warehouses[warehouse_id]
    origin, destination = _place(day, warehouse["city"]), _place(day, order["shipping_city"])
    km = _distance(origin, destination) if origin and destination else 500.0
    kind = order["customer_type"]
    is_cod = order["term_code"] == "COD"
    service = ("FULL_TRUCKLOAD" if weight >= settings.truckload_min_kg else
               "SURFACE_LTL" if weight >= settings.surface_min_kg else
               "HYPERLOCAL" if kind == "B2C" and km <= settings.hyperlocal_max_km and rng.random() < settings.hyperlocal_share
               else "EXPRESS_COURIER")
    carriers = [c for c in _carriers(day) if c["carrier_type"] == service and (c["supports_cod"] or not is_cod)]
    if not carriers:
        service = "EXPRESS_COURIER"
        carriers = [c for c in _carriers(day) if c["carrier_type"] == service and (c["supports_cod"] or not is_cod)]
    carrier = rng.choice(carriers)
    rate = settings.freight_rates[service]
    kg = float(weight)
    freight = money(Decimal(str(round(rate.base + rate.per_kg * kg + rate.per_kg_100km * kg * km / 100, 2))))
    packages = max(1, math.ceil(kg / settings.kg_per_package[kind]))

    completes = book.completes(order["order_id"])
    bill = invoicer.bill(order, warehouse_id, order["shipping_state_id"], order["shipping_address_id"], dispatched,
                         bill_lines, shipment_id=shipment_id, completes_order=completes)
    registration = Registration(warehouse["company_gst_registration_id"],
                                invoicer.registrations[warehouse["company_gst_registration_id"]]["state_code"])
    day.writer.insert("shipments", {
        "shipment_id": shipment_id, "shipment_number": day.numbers.next(day.conn, "SHP", day.day, registration),
        "order_id": order["order_id"], "warehouse_id": warehouse_id, "shipping_address_id": order["shipping_address_id"],
        "shipping_carrier_id": carrier["shipping_carrier_id"],
        "tracking_number": tracking_number(carrier["tracking_number_prefix"], shipment_id), "is_cod": is_cod,
        "cod_amount": bill.total_amount if is_cod else None, "package_count": packages,
        "total_weight_kg": max(Decimal("0.001"), weight.quantize(Decimal("0.001"))), "freight_cost_amount": freight,
        "eway_bill_number": eway_bill_number(rng) if bill.total_amount > settings.eway_bill_threshold else None,
        "planned_dispatch_date": day.day, "dispatched_at": dispatched,
        "expected_delivery_date": expected_delivery(day.day, transit_days(day, service, km)), "delivered_at": None,
        "rto_received_at": None, "shipment_status": "PICKED_UP", "created_at": created, "updated_at": dispatched,
    })
    for number, (line_id, reservation, piece) in enumerate(shipment_lines, 1):
        day.writer.insert("shipment_line_items", {
            "shipment_line_item_id": line_id, "shipment_id": shipment_id, "line_number": number,
            "order_line_item_id": reservation["order_line_item_id"], "inventory_batch_id": piece.batch_id,
            "quantity_shipped": piece.quantity, "picked_from_location_id": piece.location_id,
            "created_at": timeline[2][1], "updated_at": timeline[2][1],
        })
        day.writer.insert("inventory_transactions", ledger_row(day, warehouse_id, reservation["product_id"], piece,
                                                               dispatched, shipment_line_item_id=line_id))
    previous = None
    for status, moment in timeline:
        by = rng.choice(crew["LEAD"] if status in ("PENDING", "PICKED_UP") else crew["STAFF"])
        record_status(day, "shipment", shipment_id, previous, status, moment, by,
                      "Handed to the carrier" if status == "PICKED_UP" else None)
        previous = status
