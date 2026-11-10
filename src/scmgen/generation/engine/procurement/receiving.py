"""Receiving (1C §3.0; D31; step 3.7b): supplier deliveries → GRN + same-day dock inspection → accepted
quantity into SELLABLE stock (PURCHASE_RECEIPT, batches, inventory, P03 Dr Inventory / Cr GRNI); rejected
quantity goes back at the door with no stock movement and is re-sent later; POs short-close when the
supplier can't deliver the balance.

Each PO's delivery plan (lateness, split into parts, short-close) is drawn from a stream keyed by the PO's
own date and id (D65), so it is the same whichever run reaches the delivery. How far a PO has got is read
from its GRNs (D66): delivery k is due when the PO has k − 1 GRNs and the planned date has come.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import quantity, unit_price
from scmgen.domain.costing import StockPosition
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import record_status
from scmgen.generation.engine.procurement.sourcing import _dcs, _supplier
from scmgen.generation.numbering import Registration

MAX_DELIVERIES = 6                 # after this many GRNs a PO still missing goods is short-closed
REJECTION_REASONS = ("DAMAGED_IN_TRANSIT", "QUALITY_FAILED", "WRONG_ITEM")
SHORT_CLOSE_REASON = "Supplier unable to deliver the balance"


@dataclass(frozen=True)
class DeliveryPlan:
    dates: tuple[date, ...]            # planned date of each part
    shares: tuple[float, ...]          # share of the ordered quantity in each part but the last
    short_close: bool                  # the last part never comes


def plan_for(day: DayContext, po: dict[str, Any]) -> DeliveryPlan:
    """The PO's delivery plan, drawn from its own stream: identical on every day and in every run."""
    settings = day.world.config.procurement
    rng = day.streams.stream(po["po_date"], f"procurement.delivery.{po['purchase_order_id']}")
    rating = float(po["supplier_rating"] or 3)
    late = rng.random() < settings.late_share + 0.05 * max(0.0, 5 - rating)
    delay = rng.randint(*settings.late_days) if late else -rng.randint(0, settings.early_days)
    first = max(po["expected_delivery_date"] + timedelta(days=delay), po["po_date"] + timedelta(days=1))
    parts = rng.randint(*settings.deliveries) if rng.random() < settings.partial_delivery_share else 1
    dates = [first]
    for _ in range(parts - 1):
        dates.append(dates[-1] + timedelta(days=rng.randint(*settings.delivery_gap_days)))
    weights = [rng.uniform(1, 3) for _ in range(parts)]
    shares = tuple(w / sum(weights) for w in weights[:-1])
    return DeliveryPlan(tuple(dates), shares, parts > 1 and rng.random() < settings.short_close_share)


def _redelivery_date(day: DayContext, po: dict[str, Any], part: int) -> date:
    """A replacement part (for rejected goods) comes a few days after the last delivery."""
    rng = day.streams.stream(po["po_date"], f"procurement.delivery.{po['purchase_order_id']}.part{part}")
    return po["last_receipt_date"] + timedelta(days=rng.randint(*day.world.config.procurement.delivery_gap_days))


# ------------------------------------------------------------------------------------------ the day step


def receiving(day: DayContext) -> None:
    pos = day.rows("""
        SELECT h.purchase_order_id, h.po_date, h.po_status, h.expected_delivery_date, h.supplier_id,
               h.delivery_warehouse_id, s.supplier_rating, COUNT(g.goods_receipt_id) AS deliveries,
               MAX(g.receipt_date) AS last_receipt_date
        FROM purchase_order_headers h JOIN suppliers s ON s.supplier_id = h.supplier_id
        LEFT JOIN goods_receipts g ON g.purchase_order_id = h.purchase_order_id AND g.grn_status <> 'CANCELLED'
        WHERE h.po_status IN ('SENT', 'PARTIALLY_RECEIVED') AND h.po_date < :d
        GROUP BY h.purchase_order_id ORDER BY h.purchase_order_id""", d=day.day)
    if not pos:
        return
    settings = day.world.config.procurement
    due: list[tuple[dict[str, Any], float | None]] = []          # (PO, share of ordered qty; None = everything open)
    short: list[dict[str, Any]] = []
    for po in pos:
        plan = plan_for(day, po)
        part = po["deliveries"] + 1
        if part > MAX_DELIVERIES:
            short.append(po)
        elif part <= len(plan.dates):
            planned = plan.dates[part - 1]
            if plan.short_close and part == len(plan.dates):
                if day.day >= planned + timedelta(days=settings.short_close_wait_days):
                    short.append(po)
            elif day.day >= planned:
                due.append((po, plan.shares[part - 1] if part < len(plan.dates) else None))
        elif day.day >= _redelivery_date(day, po, part):
            due.append((po, None))
    if not due and not short:
        return
    ids = ", ".join(str(po["purchase_order_id"]) for po, _ in due) or "0"
    short_ids = ", ".join(str(po["purchase_order_id"]) for po in short) or "0"
    lines: dict[int, list[dict[str, Any]]] = {}
    for line in day.rows(f"""
            SELECT l.purchase_order_line_item_id, l.purchase_order_id, l.line_number, l.product_id, l.ordered_quantity,
                   l.unit_price, l.discount_percent, l.gst_rate, l.quantity_received, l.quantity_accepted,
                   l.quantity_rejected, l.quantity_cancelled, l.quantity_open, l.line_status,
                   p.is_batch_tracked, p.has_expiry, p.shelf_life_days, p.reorder_level, p.reorder_quantity
            FROM purchase_order_line_items l JOIN products p ON p.product_id = l.product_id
            WHERE l.purchase_order_id IN ({ids}, {short_ids}) ORDER BY l.purchase_order_id, l.line_number"""):
        lines.setdefault(line["purchase_order_id"], []).append(line)
    receiver = _Receiver(day, [l for po, _ in due for l in lines[po["purchase_order_id"]]],
                         sorted({po["delivery_warehouse_id"] for po, _ in due}))
    rng = day.rng("procurement.receiving")
    for po, share in due:
        receiver.deliver(rng, po, lines[po["purchase_order_id"]], share)
    receiver.write_inventory()
    for po in short:
        _short_close(day, po, lines[po["purchase_order_id"]])


def _line_status(line: dict[str, Any]) -> str:
    """OPEN → PARTIALLY_RECEIVED → RECEIVED (nothing more expected); CANCELLED if nothing was ever accepted."""
    if line["quantity_open"] == 0:
        return "RECEIVED" if line["quantity_accepted"] > 0 else "CANCELLED"
    return "PARTIALLY_RECEIVED" if line["quantity_received"] > 0 else "OPEN"


def _po_status(lines: list[dict[str, Any]]) -> str:
    if all(l["quantity_open"] == 0 for l in lines):
        return "RECEIVED"
    return "PARTIALLY_RECEIVED" if any(l["quantity_received"] > 0 for l in lines) else "SENT"


def _short_close(day: DayContext, po: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    stamp = day.at(18)
    for line in lines:
        if line["quantity_open"] > 0:
            line["quantity_cancelled"] += line["quantity_open"]
            line["quantity_open"] = Decimal(0)
            day.writer.update("purchase_order_line_items", "purchase_order_line_item_id", line["purchase_order_line_item_id"], {
                "quantity_cancelled": line["quantity_cancelled"], "line_status": _line_status(line), "updated_at": stamp})
    day.writer.update("purchase_order_headers", "purchase_order_id", po["purchase_order_id"], {
        "po_status": "SHORT_CLOSED", "cancellation_reason": SHORT_CLOSE_REASON, "updated_at": stamp})
    record_status(day, "purchase_order", po["purchase_order_id"], po["po_status"], "SHORT_CLOSED", stamp,
                  reason=SHORT_CLOSE_REASON)


class _Receiver:
    """Writes GRNs and inspections; keeps today's stock positions so several receipts of a product add up."""

    def __init__(self, day: DayContext, lines: list[dict[str, Any]], warehouses: list[int]) -> None:
        self.day = day
        self.book = account_book(day)
        self.positions: dict[tuple[int, int], dict[str, Any]] = {}
        in_warehouses = ", ".join(map(str, warehouses)) or "0"
        products = sorted({l["product_id"] for l in lines})
        if products:
            for row in day.rows(f"SELECT inventory_id, warehouse_id, product_id, qty_sellable, qty_quarantine, "
                                f"qty_defective, stock_value, last_unit_cost FROM inventory "
                                f"WHERE product_id IN ({', '.join(map(str, products))}) AND warehouse_id IN ({in_warehouses})"):
                on_hand = row["qty_sellable"] + row["qty_quarantine"] + row["qty_defective"]
                self.positions[(row["warehouse_id"], row["product_id"])] = {
                    **row, "position": StockPosition(on_hand, row["stock_value"], row["last_unit_cost"]),
                    "received_at": None, "new": False}
        self.storage: dict[int, list[int]] = {}
        self.receiving: dict[int, list[int]] = {}
        for row in day.rows(f"SELECT warehouse_location_id, warehouse_id, zone FROM warehouse_locations "
                            f"WHERE zone IN ('STORAGE', 'RECEIVING') AND is_active AND warehouse_id IN ({in_warehouses}) "
                            f"ORDER BY warehouse_location_id"):
            (self.storage if row["zone"] == "STORAGE" else self.receiving).setdefault(row["warehouse_id"], []).append(
                row["warehouse_location_id"])
        self.states = {r["state_id"]: r["state_code"] for r in day.rows("SELECT state_id, state_code FROM states")}
        self.crews: dict[int, list[dict[str, Any]]] = {}
        for row in day.rows(f"SELECT employee_id, warehouse_id, job_level FROM employees WHERE warehouse_id IN ({in_warehouses}) "
                            f"AND employment_status = 'ACTIVE' AND hire_date <= :d ORDER BY employee_id", d=day.day):
            self.crews.setdefault(row["warehouse_id"], []).append(row)

    def _crew(self, warehouse_id: int, levels: tuple[str, ...]) -> list[int]:
        return [e["employee_id"] for e in self.crews[warehouse_id] if e["job_level"] in levels]

    def deliver(self, rng: random.Random, po: dict[str, Any], lines: list[dict[str, Any]], share: float | None) -> None:
        day, settings = self.day, self.day.world.config.procurement
        warehouse = _dcs(day)[po["delivery_warehouse_id"]]
        delivered = []
        for line in lines:
            if line["quantity_open"] <= 0:
                continue
            qty = line["quantity_open"] if share is None else min(
                line["quantity_open"], quantity(int(line["ordered_quantity"] * Decimal(str(share)))))
            if qty > 0:
                delivered.append((line, qty))
        if not delivered:                              # a small order can't be split: the rest comes now
            delivered = [(l, l["quantity_open"]) for l in lines if l["quantity_open"] > 0]
        rating = float(po["supplier_rating"] or 3)
        grn_id = day.ids.next("goods_receipts")
        received_at = day.at(rng.randint(13, 16), rng.randint(0, 59))
        inspected_at = received_at + timedelta(minutes=rng.randint(30, 150))
        receiver = rng.choice(self._crew(warehouse["warehouse_id"], ("STAFF", "LEAD")))
        inspector = rng.choice(self._crew(warehouse["warehouse_id"], ("LEAD", "MANAGER")))
        value = sum((q * l["unit_price"] * (1 + l["gst_rate"] / 100) for l, q in delivered), Decimal(0))
        supplier_state = self.states[_supplier(day, po["supplier_id"])["registered_state_id"]]
        registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
        day.writer.insert("goods_receipts", {
            "goods_receipt_id": grn_id, "grn_number": day.numbers.next(day.conn, "GRN", day.day, registration),
            "purchase_order_id": po["purchase_order_id"], "warehouse_id": warehouse["warehouse_id"],
            "receipt_date": day.day, "received_at": received_at,
            "supplier_delivery_note_number": f"DN/{day.day:%y%m}/{rng.randint(10000, 99999)}",
            "eway_bill_number": str(rng.randint(10 ** 11, 10 ** 12 - 1)) if value > settings.eway_bill_threshold else None,
            "vehicle_number": f"{supplier_state}{rng.randint(1, 99):02d}{''.join(rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ') for _ in range(2))}"
                              f"{rng.randint(1, 9999):04d}",
            "received_by_employee_id": receiver, "grn_status": "INSPECTED",
            "created_at": received_at, "updated_at": inspected_at,
        })
        total = Decimal(0)
        for number, (line, qty) in enumerate(delivered, 1):
            total += self._receive_line(rng, po, warehouse, grn_id, number, line, qty, rating, received_at,
                                        inspected_at, inspector)
        status = _po_status(lines)
        if status != po["po_status"]:
            day.writer.update("purchase_order_headers", "purchase_order_id", po["purchase_order_id"],
                              {"po_status": status, "updated_at": inspected_at})
            record_status(day, "purchase_order", po["purchase_order_id"], po["po_status"], status, inspected_at, inspector)
            po["po_status"] = status
        if total > 0:
            journal = (JournalBuilder(f"Goods receipt {po['purchase_order_id']} at {warehouse['warehouse_id']}")
                       .debit(self.book.role("INVENTORY"), total, warehouse_id=warehouse["warehouse_id"])
                       .credit(self.book.role("GRNI"), total)   # reconciled per PO line, not by GL party (V-GRNI)
                       .build())
            post_journal(day, journal, source="GOODS_RECEIPT", goods_receipt_id=grn_id, created_by=inspector)

    def _receive_line(self, rng: random.Random, po: dict[str, Any], warehouse: dict[str, Any], grn_id: int, number: int,
                      line: dict[str, Any], qty: Decimal, rating: float, received_at: datetime, inspected_at: datetime,
                      inspector: int) -> Decimal:
        """GRN line + inspection (+ batch, ledger row and stock position for the accepted part). Returns the
        accepted value (accepted qty × PO net price)."""
        day, settings = self.day, self.day.world.config.procurement
        made = expiry = batch_ref = None
        rejected, reason = Decimal(0), None
        if line["is_batch_tracked"] or line["has_expiry"]:
            shelf = line["shelf_life_days"] or 365
            short_life = line["has_expiry"] and rng.random() < settings.short_shelf_life_share
            age = rng.uniform(0.45, 0.7) if short_life else rng.uniform(0.01, 0.25)
            made = day.day - timedelta(days=max(1, int(shelf * age)))
            expiry = made + timedelta(days=shelf) if line["has_expiry"] else None
            batch_ref = f"B{made:%y%m}{rng.randint(1000, 9999)}"
            if expiry is not None and (expiry - day.day).days < shelf * settings.min_shelf_life_share:
                rejected, reason = qty, "SHORT_SHELF_LIFE"
        if reason is None and rng.random() < settings.rejection_share + 0.01 * max(0.0, 5 - rating):
            rejected = qty if qty <= 2 else quantity(max(1, int(qty * Decimal(str(rng.uniform(0.05, 0.5))))))
            reason = rng.choice(REJECTION_REASONS)
        accepted = qty - rejected
        grn_line_id = day.ids.next("goods_receipt_line_items")
        receiving = self.receiving[warehouse["warehouse_id"]]
        day.writer.insert("goods_receipt_line_items", {
            "goods_receipt_line_item_id": grn_line_id, "goods_receipt_id": grn_id, "line_number": number,
            "purchase_order_line_item_id": line["purchase_order_line_item_id"], "quantity_received": qty,
            "receiving_location_id": receiving[line["product_id"] % len(receiving)],
            "supplier_batch_number": batch_ref, "manufacture_date": made, "expiry_date": expiry,
            "created_at": received_at, "updated_at": received_at,
        })
        day.writer.insert("goods_inspections", {
            "goods_inspection_id": day.ids.next("goods_inspections"), "goods_receipt_line_item_id": grn_line_id,
            "inspected_at": inspected_at, "inspected_by_employee_id": inspector, "quantity_accepted": accepted,
            "quantity_rejected": rejected,
            "inspection_result": "ACCEPTED" if rejected == 0 else ("REJECTED" if accepted == 0 else "PARTIALLY_ACCEPTED"),
            "rejection_reason": reason, "inspection_notes": None, "created_at": inspected_at, "updated_at": inspected_at,
        })
        line["quantity_received"] += qty
        line["quantity_accepted"] += accepted
        line["quantity_rejected"] += rejected
        line["quantity_open"] = line["ordered_quantity"] - line["quantity_accepted"] - line["quantity_cancelled"]
        day.writer.update("purchase_order_line_items", "purchase_order_line_item_id", line["purchase_order_line_item_id"], {
            "quantity_received": line["quantity_received"], "quantity_accepted": line["quantity_accepted"],
            "quantity_rejected": line["quantity_rejected"], "line_status": _line_status(line), "updated_at": inspected_at})
        if accepted == 0:
            return Decimal(0)                          # rejected goods go back at the door: no stock movement (D31)

        batch_id = None
        if line["is_batch_tracked"]:
            batch_id = day.ids.next("inventory_batches")
            day.writer.insert("inventory_batches", {
                "inventory_batch_id": batch_id, "batch_number": f"LOT-GRN-{grn_id:08d}-{number:03d}",
                "product_id": line["product_id"], "goods_receipt_line_item_id": grn_line_id,
                "stock_adjustment_line_item_id": None, "supplier_batch_number": batch_ref, "manufacture_date": made,
                "expiry_date": expiry, "received_date": day.day, "initial_quantity": accepted,
                "created_at": inspected_at, "updated_at": inspected_at,
            })
        key = (warehouse["warehouse_id"], line["product_id"])
        state = self.positions.get(key)
        if state is None:                              # first receipt of a new product at this DC
            level, reorder = line["reorder_level"], line["reorder_quantity"]
            state = self.positions[key] = {"inventory_id": day.ids.next("inventory"), "position": StockPosition(),
                                           "new": True, "reorder_level": level, "reorder_quantity": reorder,
                                           "max_stock_level": level + reorder, "created_at": inspected_at}
        net_price = unit_price(line["unit_price"] * (1 - line["discount_percent"] / 100))
        state["position"], movement = state["position"].receive(accepted, net_price)
        state["received_at"] = inspected_at
        storage = self.storage[warehouse["warehouse_id"]]
        day.writer.insert("inventory_transactions", {
            "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": inspected_at,
            "warehouse_id": warehouse["warehouse_id"], "product_id": line["product_id"], "inventory_batch_id": batch_id,
            "warehouse_location_id": storage[line["product_id"] % len(storage)],
            "transaction_type": "PURCHASE_RECEIPT", "stock_status": "SELLABLE", "quantity": movement.quantity,
            "unit_cost": movement.unit_cost, "value_change": movement.value_change,
            "goods_receipt_line_item_id": grn_line_id, "shipment_line_item_id": None, "invoice_line_item_id": None,
            "sales_return_line_item_id": None, "return_inspection_id": None, "stock_transfer_line_item_id": None,
            "stock_adjustment_line_item_id": None, "defective_stock_id": None,
            "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
            "created_at": inspected_at, "updated_at": inspected_at,
        })
        return movement.value_change

    def write_inventory(self) -> None:
        """One insert or update per warehouse × product received today (sellable qty and value)."""
        for (warehouse_id, product_id), state in sorted(self.positions.items()):
            if state["received_at"] is None:
                continue
            position: StockPosition = state["position"]
            if state["new"]:
                self.day.writer.insert("inventory", {
                    "inventory_id": state["inventory_id"], "warehouse_id": warehouse_id, "product_id": product_id,
                    "qty_sellable": position.quantity, "qty_quarantine": Decimal(0), "qty_defective": Decimal(0),
                    "qty_reserved": Decimal(0), "stock_value": position.value, "last_unit_cost": position.last_unit_cost,
                    "reorder_level": state["reorder_level"], "reorder_quantity": state["reorder_quantity"],
                    "max_stock_level": state["max_stock_level"], "is_stocked": True,
                    "last_receipt_at": state["received_at"], "last_issue_at": None, "last_counted_at": None,
                    "created_at": state["created_at"], "updated_at": state["received_at"],
                })
            else:
                sellable = position.quantity - state["qty_quarantine"] - state["qty_defective"]
                self.day.writer.update("inventory", "inventory_id", state["inventory_id"], {
                    "qty_sellable": sellable, "stock_value": position.value, "last_unit_cost": position.last_unit_cost,
                    "last_receipt_at": state["received_at"], "updated_at": state["received_at"]})
