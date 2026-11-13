"""Stock transfers between warehouses (1C §5 ``stock_transfers``; D29, D55; 1E P16, P17; step 3.12a).

* **Store replenishment** (weekly, each store on its own weekday): a store line whose available stock (sellable
  − reserved − expired + inbound transfers − backordered store orders) is below its reorder level is refilled to
  its maximum from the store's supplying DC (``warehouses.replenishment_warehouse_id``), as far as the DC has
  unreserved, unexpired stock. Stores sit in their DC's state: one GSTIN, a delivery challan, no tax (D29).
  Own vehicle, the next working day.
* **DC rebalancing** (monthly, first working day on or after ``inventory.rebalancing_day``): a DC whose available
  stock of a product is below its reorder level gets the excess above the maximum of the DC holding the most.
  Every DC has its own state's GSTIN, so the transfer is a taxable supply: the transfer document is the tax
  invoice (``tax_invoice_number``, T series of the source GSTIN), IGST at the product's rate on its moving-average
  value, two tax rows per line (OUTPUT at the source, INPUT at the destination). Surface or full-truckload
  carrier, transit time by distance.
* **Dispatch** in the evening, after the day's picking and receipts (each transfer starts after the last movement
  of its products at the source, so the moving-average replay of V-IN-09 sees events in execution order):
  TRANSFER_OUT at the source MAC (FEFO batches with ``min_shelf_days`` left on arrival, one line per batch), P16
  Dr Inventory in Transit / Cr Inventory (source), and for a taxable transfer Dr Input IGST (destination GSTIN) /
  Cr Output IGST (source GSTIN) (D55: no branch accounts).
* **Receipt** first thing on the arrival day (``transfer_receipts_step`` runs before procurement, so it precedes
  every other stock movement of the day): TRANSFER_IN of the good quantity (SELLABLE) and of goods damaged in
  transit (DEFECTIVE, with a ``defective_stock`` entry, TRANSIT_DAMAGE); lost units have no ledger row. P17
  Dr Inventory (destination) at the transfer cost, Dr Inventory Write-offs for the lost value / Cr Inventory in
  Transit. The values of a line's buckets add up to its transfer value exactly (the last bucket takes the paise).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO, money, product, quantity, total
from scmgen.domain.gst import split_gst
from scmgen.domain.identifiers import eway_bill_number
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver, preload_rates, rate_on
from scmgen.generation.engine.fulfilment.dispatch import _carriers, _place, expected_delivery, transit_days
from scmgen.generation.engine.fulfilment.stock import Issuer, Piece
from scmgen.generation.engine.sales.allocation import _distance, expired_stock
from scmgen.generation.numbering import Registration

OPEN_STATUSES = ("APPROVED", "IN_TRANSIT")


@dataclass
class _Need:
    product_id: int
    quantity: Decimal


@dataclass
class _Plan:
    transfer_type: str
    source: int
    destination: int
    needs: list[_Need] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ master data


def warehouses(day: DayContext) -> dict[int, dict[str, Any]]:
    """Warehouses with their GSTIN, state and cost centre (cached for the run)."""
    refs = day.world.refs
    if "transfer_warehouses" not in refs:
        refs["transfer_warehouses"] = {r["warehouse_id"]: r for r in day.rows("""
            SELECT w.warehouse_id, w.warehouse_code, w.warehouse_type, w.city, w.replenishment_warehouse_id,
                   w.company_gst_registration_id, g.gstin, g.state_id, s.state_code, c.cost_center_id
            FROM warehouses w JOIN company_gst_registrations g
                 ON g.company_gst_registration_id = w.company_gst_registration_id
            JOIN states s ON s.state_id = g.state_id
            JOIN cost_centers c ON c.warehouse_id = w.warehouse_id
            WHERE w.warehouse_status = 'ACTIVE' ORDER BY w.warehouse_id""")}
    return refs["transfer_warehouses"]


def _locations(day: DayContext, warehouse_id: int, zones: tuple[str, ...]) -> list[int]:
    """Active locations of the first zone the warehouse has, in id order."""
    cache = day.world.refs.setdefault("transfer_locations", {})
    key = (warehouse_id, zones)
    if key not in cache:
        rows = day.rows(f"""SELECT warehouse_location_id, zone FROM warehouse_locations
                            WHERE warehouse_id = :w AND is_active AND zone IN ({", ".join(f"'{z}'" for z in zones)})
                            ORDER BY warehouse_location_id""", w=warehouse_id)
        for zone in zones:
            found = [r["warehouse_location_id"] for r in rows if r["zone"] == zone]
            if found:
                cache[key] = found
                break
        else:
            raise RuntimeError(f"Warehouse {warehouse_id} has no location in {zones}")
    return cache[key]


def shelf_location(day: DayContext, warehouse: dict[str, Any], product_id: int) -> int:
    zone = "SALES_FLOOR" if warehouse["warehouse_type"] == "RETAIL_STORE" else "STORAGE"
    locations = _locations(day, warehouse["warehouse_id"], (zone,))
    return locations[product_id % len(locations)]


def defective_location(day: DayContext, warehouse_id: int) -> int:
    return _locations(day, warehouse_id, ("DEFECTIVE", "QUARANTINE"))[0]


def _crew(day: DayContext, warehouse_id: int) -> list[int]:
    key = ("transfer_crew", warehouse_id, day.day)
    if key not in day.notes:
        day.notes[key] = [r["employee_id"] for r in day.rows("""
            SELECT employee_id FROM employees WHERE warehouse_id = :w AND employment_status = 'ACTIVE'
              AND hire_date <= :d AND job_level IN ('STAFF', 'LEAD', 'MANAGER') ORDER BY employee_id""",
            w=warehouse_id, d=day.day)]
    return day.notes[key]


def _inbound(day: DayContext, destinations: list[int]) -> dict[tuple[int, int], Decimal]:
    """Quantity on its way to each warehouse × product (open transfers)."""
    if not destinations:
        return {}
    return {(r["destination_warehouse_id"], r["product_id"]): r["qty"] for r in day.rows(f"""
        SELECT t.destination_warehouse_id, l.product_id, SUM(l.quantity_requested) AS qty
        FROM stock_transfers t JOIN stock_transfer_line_items l ON l.stock_transfer_id = t.stock_transfer_id
        WHERE t.transfer_status IN ({", ".join(f"'{s}'" for s in OPEN_STATUSES)})
          AND t.destination_warehouse_id IN ({", ".join(map(str, destinations))})
        GROUP BY 1, 2""")}


def inbound_transfers(day: DayContext, warehouse_ids: list[int]) -> dict[tuple[int, int], Decimal]:
    """Public view for procurement: stock already on its way to these warehouses by transfer."""
    return _inbound(day, warehouse_ids)


# ------------------------------------------------------------------------------------------ planning


def _rebalancing_today(day: DayContext) -> bool:
    """The first working day on or after the configured day of the month."""
    first = day.day.replace(day=day.world.config.inventory.rebalancing_day)
    if day.day < first:
        return False
    probe = first
    while not day.world.config.daily.is_working_day(probe):
        probe += timedelta(days=1)
    return probe == day.day


def _available(day: DayContext, warehouse_ids: list[int], where: str = "") -> list[dict[str, Any]]:
    return day.rows(f"""
        SELECT i.warehouse_id, i.product_id, i.qty_sellable - i.qty_reserved AS available, i.reorder_level,
               i.max_stock_level
        FROM inventory i JOIN products p ON p.product_id = i.product_id
        WHERE i.warehouse_id IN ({", ".join(map(str, warehouse_ids))}) AND i.is_stocked
          AND p.product_status = 'ACTIVE' {where}
        ORDER BY i.warehouse_id, i.product_id""")


def _store_plans(day: DayContext, sites: dict[int, dict[str, Any]]) -> list[_Plan]:
    stores = [w for w in sites.values() if w["warehouse_type"] == "RETAIL_STORE"
              and w["warehouse_id"] % 6 == day.day.weekday()]
    if not stores:
        return []
    ids = [s["warehouse_id"] for s in stores]
    expired = expired_stock(day)
    inbound = _inbound(day, ids)
    waiting: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for row in day.rows(f"""
            SELECT o.store_warehouse_id, l.product_id, SUM(l.quantity_ordered - l.quantity_cancelled) AS qty
            FROM order_line_items l JOIN orders o ON o.order_id = l.order_id
            WHERE l.line_status = 'BACKORDERED' AND o.fulfilment_type <> 'SHIP'
              AND o.store_warehouse_id IN ({", ".join(map(str, ids))}) GROUP BY 1, 2"""):
        waiting[(row["store_warehouse_id"], row["product_id"])] += row["qty"]
    plans = {s["warehouse_id"]: _Plan("STORE_REPLENISHMENT", s["replenishment_warehouse_id"], s["warehouse_id"])
             for s in stores}
    for row in _available(day, ids):
        key = (row["warehouse_id"], row["product_id"])
        position = row["available"] - expired.get(key, ZERO) + inbound.get(key, ZERO) - waiting.get(key, ZERO)
        if position < row["reorder_level"]:
            need = quantity(row["max_stock_level"] - position)
            if need > 0:
                plans[row["warehouse_id"]].needs.append(_Need(row["product_id"], need))
    return [p for _, p in sorted(plans.items()) if p.needs]


def _rebalancing_plans(day: DayContext, sites: dict[int, dict[str, Any]]) -> list[_Plan]:
    dcs = sorted(w for w, site in sites.items() if site["warehouse_type"] == "DISTRIBUTION_CENTRE")
    if len(dcs) < 2 or not _rebalancing_today(day):
        return []
    expired = expired_stock(day)
    rows = _available(day, dcs)
    short, excess = [], defaultdict(list)
    for row in rows:
        key = (row["warehouse_id"], row["product_id"])
        free = row["available"] - expired.get(key, ZERO)
        if free < row["reorder_level"]:
            short.append((row, quantity(row["max_stock_level"] - free)))
        elif free > row["max_stock_level"]:
            excess[row["product_id"]].append([row["warehouse_id"], quantity(free - row["max_stock_level"])])
    plans: dict[tuple[int, int], _Plan] = {}
    for row, need in short:
        donors = sorted((d for d in excess.get(row["product_id"], []) if d[1] > 0 and d[0] != row["warehouse_id"]),
                        key=lambda d: (-d[1], d[0]))
        if not donors or need <= 0:
            continue
        donor = donors[0]
        qty = min(need, donor[1])
        donor[1] -= qty
        plan = plans.setdefault((donor[0], row["warehouse_id"]), _Plan("REBALANCING", donor[0], row["warehouse_id"]))
        plan.needs.append(_Need(row["product_id"], qty))
    return [p for _, p in sorted(plans.items())]


# ------------------------------------------------------------------------------------------ dispatch


def dispatch_step(day: DayContext) -> None:
    """Plan today's store replenishment and DC rebalancing and dispatch them in the evening (P16)."""
    if not day.is_working_day:
        return
    sites = warehouses(day)
    plans = _store_plans(day, sites) + _rebalancing_plans(day, sites)
    if not plans:
        return
    settings = day.world.config.inventory
    issuer = Issuer(day, {(p.source, n.product_id) for p in plans for n in p.needs})
    products = {r["product_id"]: r for r in day.rows(f"""
        SELECT p.product_id, p.hsn_code, p.weight_kg, u.gst_uqc FROM products p
        JOIN product_units_of_measure u ON u.unit_of_measure_id = p.base_unit_of_measure_id
        WHERE p.product_id IN ({", ".join(sorted({str(n.product_id) for p in plans for n in p.needs}))})""")}
    preload_rates(day, products, day.day)
    expired = expired_stock(day)
    book = account_book(day)
    # Split plans into documents and time them; then dispatch in time order (MAC, as for sales dispatches).
    documents = []
    for plan in plans:
        for start in range(0, len(plan.needs), settings.transfer_max_lines):
            needs = plan.needs[start:start + settings.transfer_max_lines]
            rng = day.rng(f"inventory.transfer.{plan.source}.{plan.destination}.{start}")
            at = day.at(rng.randint(*settings.transfer_dispatch_hours), rng.randint(0, 59), rng.randint(0, 59))
            moved = [issuer.last_moved_at(plan.source, n.product_id) for n in needs
                     if (plan.source, n.product_id) in issuer.positions]
            at = max([at] + [m + timedelta(minutes=10) for m in moved if m is not None])
            if at.date() == day.day:
                documents.append((at, plan, needs, rng))
    taken: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for at, plan, needs, rng in sorted(documents, key=lambda d: (d[0], d[1].source, d[1].destination)):
        _dispatch(day, book, sites, issuer, products, expired, taken, plan, needs, at, rng)
    issuer.write(reserved=False)


def _transfer_quantity(day: DayContext, issuer: Issuer, expired: dict, taken: dict, source: int, need: _Need,
                       usable_on: date) -> Decimal:
    key = (source, need.product_id)
    state = issuer.positions.get(key)
    if state is None:
        return ZERO
    free = state["qty_sellable"] - state["qty_reserved"] - expired.get(key, ZERO) - taken[key]
    qty = min(need.quantity, quantity(free))
    qty = issuer.pickable(source, need.product_id, qty, usable_on) if qty > 0 else ZERO
    if qty > 0 and qty >= issuer.on_hand(source, need.product_id):
        qty -= 1                     # never empty the position: its value then equals ROUND(q × MAC) exactly
    return qty.to_integral_value() if qty > 0 else ZERO


def _dispatch(day: DayContext, book, sites: dict[int, dict[str, Any]], issuer: Issuer,
              products: dict[int, dict[str, Any]], expired: dict, taken: dict, plan: _Plan, needs: list[_Need],
              dispatched: datetime, rng) -> None:
    settings = day.world.config.inventory
    source, destination = sites[plan.source], sites[plan.destination]
    taxable = source["company_gst_registration_id"] != destination["company_gst_registration_id"]
    origin, target = _place(day, source["city"]), _place(day, destination["city"])
    km = _distance(origin, target) if origin and target else 500.0
    weight = Decimal(0)
    if plan.transfer_type == "STORE_REPLENISHMENT":
        carrier, arrival = None, _next_working_day(day, day.day + timedelta(days=1))
    else:
        carrier, arrival = None, None              # chosen once the weight is known
    usable_on = (arrival or day.day + timedelta(days=7)) + timedelta(days=settings.min_shelf_days)
    transfer_id = day.ids.next("stock_transfers")
    lines = []
    for need in needs:
        qty = _transfer_quantity(day, issuer, expired, taken, plan.source, need, usable_on)
        if qty <= 0:
            continue
        taken[(plan.source, need.product_id)] += qty
        pieces: list[Piece] = issuer.issue(plan.source, need.product_id, qty, dispatched, usable_on=usable_on)
        for piece in pieces:
            lines.append((need.product_id, piece))
        weight += qty * products[need.product_id]["weight_kg"]
    if not lines:
        return
    if plan.transfer_type == "REBALANCING":
        service = "FULL_TRUCKLOAD" if weight >= day.world.config.fulfilment.truckload_min_kg else "SURFACE_LTL"
        choices = [c for c in _carriers(day) if c["carrier_type"] == service]
        carrier = rng.choice(choices)["shipping_carrier_id"] if choices else None
        arrival = _next_working_day(day, expected_delivery(day.day, transit_days(day, service, km)))
    # Everything that queries (approver, crew, numbers) comes before the first buffered row: a query flushes the
    # buffer, and the ledger rows must not reach the database before their transfer lines.
    value_estimate = total(money(product(piece.quantity, piece.movement.unit_cost)) for _, piece in lines)
    approved_by = approver(day, value_estimate, ("WHO", "LOG"), day.day)
    created_by = rng.choice(_crew(day, plan.source))
    registration = Registration(source["company_gst_registration_id"], source["state_code"])
    number = day.numbers.next(day.conn, "STO", day.day, registration)
    tax_number = day.numbers.next(day.conn, "TRANSFER_INVOICE", day.day, registration) if taxable else None
    rows, values, igst_total = [], [], ZERO
    for line_number, (product_id, piece) in enumerate(lines, 1):
        cost = piece.movement.unit_cost
        value = money(product(piece.quantity, cost))
        if value != -piece.movement.value_change:
            raise RuntimeError(f"Transfer value {value} differs from the issue value {piece.movement.value_change}")
        rate = rate_on(day, product_id, day.day) if taxable else ZERO
        igst = split_gst(value, rate, True).igst if taxable else ZERO
        line_id = day.ids.next("stock_transfer_line_items")
        rows.append({
            "stock_transfer_line_item_id": line_id, "stock_transfer_id": transfer_id, "line_number": line_number,
            "product_id": product_id, "inventory_batch_id": piece.batch_id, "stock_status": "SELLABLE",
            "quantity_requested": piece.quantity, "quantity_shipped": piece.quantity, "quantity_received_good": ZERO,
            "quantity_received_damaged": ZERO, "quantity_lost": ZERO, "unit_cost": cost, "transfer_value": value,
            "gst_rate": rate, "igst_amount": igst, "created_at": dispatched, "updated_at": dispatched,
        })
        values.append(value)
        igst_total += igst
        day.writer.insert("inventory_transactions", ledger_row(
            day, "TRANSFER_OUT", plan.source, product_id, piece.batch_id, piece.location_id, "SELLABLE",
            piece.movement.quantity, cost, piece.movement.value_change, line_id, dispatched))
        if taxable:
            for direction, kind, own, other in (("OUTPUT", "TRANSFER_OUT", source, destination),
                                                ("INPUT", "TRANSFER_IN", destination, source)):
                day.writer.insert("tax_transactions", {
                    "tax_transaction_id": day.ids.next("tax_transactions"),
                    "company_gst_registration_id": own["company_gst_registration_id"], "tax_direction": direction,
                    "document_type": kind, "is_cancellation": False, "invoice_line_item_id": None,
                    "credit_note_line_item_id": None, "supplier_invoice_line_item_id": None,
                    "expense_line_item_id": None, "stock_transfer_line_item_id": line_id,
                    "document_number": tax_number, "document_date": day.day, "counterparty_gstin": other["gstin"],
                    "customer_id": None, "supplier_id": None, "place_of_supply_state_id": destination["state_id"],
                    "is_inter_state": True, "hsn_code": products[product_id]["hsn_code"],
                    "uqc": products[product_id]["gst_uqc"], "quantity": piece.quantity, "taxable_value": value,
                    "gst_rate": rate, "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": igst,
                    "itc_eligibility": "NOT_APPLICABLE" if direction == "OUTPUT" else "ELIGIBLE",
                    "gstr1_section": "B2B" if direction == "OUTPUT" else None, "gst_return_id": None,
                    "created_at": dispatched, "updated_at": dispatched,
                })
    value_total = total(values)
    created = dispatched - timedelta(hours=rng.randint(2, 4), minutes=rng.randint(0, 59))
    eway_limit = day.world.config.fulfilment.eway_bill_threshold
    day.writer.insert("stock_transfers", {
        "stock_transfer_id": transfer_id, "transfer_number": number, "transfer_type": plan.transfer_type,
        "source_warehouse_id": plan.source, "destination_warehouse_id": plan.destination, "is_inter_gstin": taxable,
        "tax_invoice_number": tax_number,
        "eway_bill_number": eway_bill_number(rng) if value_total + igst_total > eway_limit else None,
        "shipping_carrier_id": carrier, "requested_date": day.day, "dispatched_at": dispatched,
        "expected_arrival_date": arrival, "received_at": None, "taxable_amount": value_total if taxable else ZERO,
        "igst_amount": igst_total, "transfer_status": "IN_TRANSIT", "created_by_employee_id": created_by,
        "approved_by_employee_id": approved_by,
        "received_by_employee_id": None, "created_at": created, "updated_at": dispatched,
    })
    for row in rows:
        day.writer.insert("stock_transfer_line_items", row)
    journal = (JournalBuilder(f"Transfer {number} dispatched")
               .debit(book.role("INVENTORY_IN_TRANSIT"), value_total)
               .credit(book.role("INVENTORY"), value_total, warehouse_id=plan.source))
    if taxable and igst_total > 0:
        journal.debit(book.tax(destination["company_gst_registration_id"], "IGST", "INPUT"), igst_total)
        journal.credit(book.tax(source["company_gst_registration_id"], "IGST", "OUTPUT"), igst_total)
    post_journal(day, journal.build(), source="TRANSFER_DISPATCH", stock_transfer_id=transfer_id)


def _next_working_day(day: DayContext, on: date) -> date:
    while not day.world.config.daily.is_working_day(on):
        on += timedelta(days=1)
    return on


def ledger_row(day: DayContext, kind: str, warehouse_id: int, product_id: int, batch_id: int | None,
               location_id: int, status: str, qty: Decimal, cost: Decimal, value: Decimal, line_id: int,
               at: datetime) -> dict[str, Any]:
    return {
        "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
        "warehouse_id": warehouse_id, "product_id": product_id, "inventory_batch_id": batch_id,
        "warehouse_location_id": location_id, "transaction_type": kind, "stock_status": status, "quantity": qty,
        "unit_cost": cost, "value_change": value, "goods_receipt_line_item_id": None, "shipment_line_item_id": None,
        "invoice_line_item_id": None, "sales_return_line_item_id": None, "return_inspection_id": None,
        "stock_transfer_line_item_id": line_id, "stock_adjustment_line_item_id": None, "defective_stock_id": None,
        "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
        "created_at": at, "updated_at": at,
    }


# ------------------------------------------------------------------------------------------ receipt


def _split(qty: Decimal, cost: Decimal, value: Decimal, rng, settings) -> tuple[Decimal, Decimal, Decimal]:
    """Good, damaged and lost quantity of one arriving line (drawn per line)."""
    damaged = lost = ZERO
    if rng.random() < settings.transit_damage_share:
        damaged = Decimal(rng.randint(1, max(1, int(qty) // 4))) if qty >= 1 else qty
    if rng.random() < settings.transit_loss_share:
        lost = min(qty - damaged, Decimal(rng.randint(1, max(1, int(qty) // 10))) if qty >= 1 else qty - damaged)
    return qty - damaged - lost, damaged, max(lost, ZERO)


def transfer_receipts_step(day: DayContext) -> None:
    """Transfers due today are unloaded before the warehouse day (P17); damaged units go to DEFECTIVE."""
    if not day.is_working_day:
        return
    transfers = day.rows("""
        SELECT t.stock_transfer_id, t.transfer_number, t.source_warehouse_id, t.destination_warehouse_id,
               t.dispatched_at FROM stock_transfers t
        WHERE t.transfer_status = 'IN_TRANSIT' AND t.expected_arrival_date <= :d ORDER BY t.stock_transfer_id""",
        d=day.day)
    if not transfers:
        return
    settings = day.world.config.inventory
    sites = warehouses(day)
    book = account_book(day)
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows(f"""
            SELECT l.* FROM stock_transfer_line_items l
            WHERE l.stock_transfer_id IN ({", ".join(str(t["stock_transfer_id"]) for t in transfers)})
            ORDER BY l.stock_transfer_id, l.line_number"""):
        lines[line["stock_transfer_id"]].append(line)
    changes: dict[tuple[int, int], dict[str, Any]] = {}
    for transfer in transfers:
        rng = day.streams.stream(transfer["dispatched_at"].date(), f"inventory.receipt.{transfer['stock_transfer_id']}")
        destination = sites[transfer["destination_warehouse_id"]]
        at = max(day.at(rng.randint(*settings.transfer_receipt_hours), rng.randint(0, 59), rng.randint(0, 59)),
                 transfer["dispatched_at"] + timedelta(hours=1))
        stocked, lost_value = ZERO, ZERO
        for line in lines[transfer["stock_transfer_id"]]:
            good, damaged, lost = _split(line["quantity_shipped"], line["unit_cost"], line["transfer_value"], rng,
                                         settings)
            cost = line["unit_cost"]
            values = {"good": money(product(good, cost)), "damaged": money(product(damaged, cost)),
                      "lost": money(product(lost, cost))}
            last = "damaged" if damaged else "good" if good else "lost"
            values[last] = line["transfer_value"] - sum(v for k, v in values.items() if k != last)
            for bucket, qty, status in (("good", good, "SELLABLE"), ("damaged", damaged, "DEFECTIVE")):
                if qty <= 0:
                    continue
                location = (shelf_location(day, destination, line["product_id"]) if status == "SELLABLE"
                            else defective_location(day, destination["warehouse_id"]))
                day.writer.insert("inventory_transactions", ledger_row(
                    day, "TRANSFER_IN", destination["warehouse_id"], line["product_id"], line["inventory_batch_id"],
                    location, status, qty, cost, values[bucket], line["stock_transfer_line_item_id"], at))
            if damaged:
                day.writer.insert("defective_stock", {
                    "defective_stock_id": day.ids.next("defective_stock"),
                    "defect_number": day.numbers.next(day.conn, "DEF", day.day, Registration(
                        destination["company_gst_registration_id"], destination["state_code"])),
                    "warehouse_id": destination["warehouse_id"], "product_id": line["product_id"],
                    "inventory_batch_id": line["inventory_batch_id"], "quantity": damaged,
                    "return_inspection_id": None, "stock_adjustment_line_item_id": None,
                    "stock_transfer_line_item_id": line["stock_transfer_line_item_id"],
                    "defect_reason": "TRANSIT_DAMAGE", "detected_at": at, "disposition": "PENDING",
                    "disposition_status": "PENDING", "disposed_at": None, "disposed_by_employee_id": None,
                    "supplier_id": None, "repair_cost_amount": None, "created_at": at, "updated_at": at,
                })
            day.writer.update("stock_transfer_line_items", "stock_transfer_line_item_id",
                              line["stock_transfer_line_item_id"], {
                                  "quantity_received_good": good, "quantity_received_damaged": damaged,
                                  "quantity_lost": lost, "updated_at": at})
            change = changes.setdefault((destination["warehouse_id"], line["product_id"]), {
                "good": ZERO, "damaged": ZERO, "value": ZERO, "at": at, "cost": cost})
            change["good"] += good
            change["damaged"] += damaged
            change["value"] += values["good"] + values["damaged"]
            change["at"], change["cost"] = max(change["at"], at), cost
            stocked += values["good"] + values["damaged"]
            lost_value += values["lost"]
        day.writer.update("stock_transfers", "stock_transfer_id", transfer["stock_transfer_id"], {
            "transfer_status": "RECEIVED", "received_at": at,
            "received_by_employee_id": rng.choice(_crew(day, destination["warehouse_id"])), "updated_at": at})
        journal = JournalBuilder(f"Transfer {transfer['transfer_number']} received")
        journal.debit(book.role("INVENTORY"), stocked, warehouse_id=destination["warehouse_id"])
        journal.debit(book.role("INVENTORY_WRITE_OFF"), lost_value, cost_center_id=destination["cost_center_id"])
        journal.credit(book.role("INVENTORY_IN_TRANSIT"), stocked + lost_value)
        post_journal(day, journal.build(), source="TRANSFER_RECEIPT", stock_transfer_id=transfer["stock_transfer_id"])
    if changes:
        day.execute("""
            UPDATE inventory SET qty_sellable = qty_sellable + :g, qty_defective = qty_defective + :d,
                   stock_value = stock_value + :v, last_unit_cost = IF(:g > 0, :c, last_unit_cost),
                   last_receipt_at = GREATEST(COALESCE(last_receipt_at, :t), :t), updated_at = GREATEST(updated_at, :t)
            WHERE warehouse_id = :w AND product_id = :p""",
            [{"g": c["good"], "d": c["damaged"], "v": c["value"], "c": c["cost"], "t": c["at"], "w": w, "p": p}
             for (w, p), c in sorted(changes.items())])
