"""Customer returns (1D §6; D44, D45; 1C §5.0 RETURN_RECEIPT / RETURN_DISPOSITION; 1E P11, P12; step 3.11a).

* **Requests.** Whether a delivered line comes back is decided once per invoice line from a stream keyed by the
  line (D65): the pack's return rate for the customer type (non-returnable products rarely), a reason, a quantity
  (often part of the line) and a request day within the category's return window (a few too late). The plans of
  recently delivered lines are kept in ``world.refs``, rebuilt from the delivered lines at run start (D66).
  Requests of one order on one day form one return (RMA).
* **Approval.** Customer Service decides within hours: requests too late or for non-returnable products are
  REJECTED (no goods move); otherwise APPROVED (the order lines' return-requested counters).
* **Goods back.** Store customers may drop the goods at their store; others are collected by a reverse-pickup
  carrier (PICKUP_SCHEDULED → IN_TRANSIT) and travel to the shipment's warehouse. On arrival (RECEIVED) the
  received quantity goes into QUARANTINE at the original issue cost (RETURN_RECEIPT, its batch), P12 Dr Inventory /
  Cr COGS. Receipts run in the fulfilment step's time-ordered stock pass (``receipts_due`` / ``receive``).
* **Inspection** 1–3 days later moves the quantity to SELLABLE or DEFECTIVE (two RETURN_DISPOSITION rows per bucket,
  no value change) by the reason's odds; ~5% need a further inspection; defective quantities get a
  ``defective_stock`` row (D45, disposition PENDING until the warehouse decides, 3.12).
* **Credit note** after the final inspection, for all received quantity (D44, P11) at the invoice line's price and
  rate; the credit is applied to the invoice if it is still open. A return is CLOSED once credited and, when the
  customer had paid, refunded (3.11b).
* Delivered orders are CLOSED once the return windows of their lines are over and nothing is being returned.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money, product
from scmgen.domain.gst import price_exclusive, price_inclusive
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver, record_status, staff
from scmgen.generation.engine.fulfilment.credit_notes import issue_credit_note, load_invoices
from scmgen.generation.engine.fulfilment.dispatch import _place, transit_days
from scmgen.generation.engine.sales.allocation import _distance
from scmgen.generation.numbering import Registration

LOOKBACK_DAYS = 45                 # requests come within the return window (+ a few days late)
REASONS_B2C = {"SIZE_OR_FIT": 0.25, "CHANGED_MIND": 0.20, "NOT_AS_DESCRIBED": 0.15, "DEFECTIVE": 0.15,
               "DAMAGED_IN_TRANSIT": 0.08, "WRONG_ITEM": 0.07, "WARRANTY_CLAIM": 0.05, "NEAR_EXPIRY": 0.05}
REASONS_B2B = {"DEFECTIVE": 0.35, "DAMAGED_IN_TRANSIT": 0.25, "WRONG_ITEM": 0.20, "NEAR_EXPIRY": 0.10,
               "WARRANTY_CLAIM": 0.10}
DEFECTS = {"DEFECTIVE": "MANUFACTURING_DEFECT", "DAMAGED_IN_TRANSIT": "PHYSICAL_DAMAGE",
           "WARRANTY_CLAIM": "MANUFACTURING_DEFECT", "NEAR_EXPIRY": "EXPIRED", "WRONG_ITEM": "PHYSICAL_DAMAGE",
           "NOT_AS_DESCRIBED": "USED_OR_SOILED", "CHANGED_MIND": "USED_OR_SOILED", "SIZE_OR_FIT": "USED_OR_SOILED"}


@dataclass(frozen=True)
class LinePlan:
    invoice_line_item_id: int
    request_day: date
    reason: str
    quantity: Decimal
    late: bool


def _plan(day: DayContext, line: dict[str, Any]) -> LinePlan | None:
    settings = day.world.config.returns
    pack = day.world.packs_by_code.get(line["pack_code"])
    rng = day.streams.stream(line["delivered_on"], f"returns.line.{line['invoice_line_item_id']}")
    rate = float(pack.returns["b2c_return_rate" if line["customer_type"] == "B2C" else "b2b_return_rate"]) if pack else 0.02
    if not line["is_returnable"]:
        rate *= settings.non_returnable_attempt_factor
    if rng.random() >= rate:
        return None
    reasons = REASONS_B2C if line["customer_type"] == "B2C" else REASONS_B2B
    defective = float(pack.returns["defective_share"]) if pack else 0.3
    weights = {r: w * (2 * defective if r in ("DEFECTIVE", "WARRANTY_CLAIM") else 1) for r, w in reasons.items()}
    if not line["has_expiry"]:
        weights.pop("NEAR_EXPIRY", None)
    reason = rng.choices(list(weights), weights=list(weights.values()))[0]
    qty = line["quantity"]
    if qty > 1 and rng.random() < settings.partial_quantity_share:
        qty = Decimal(rng.randint(1, int(qty) - 1)) if qty == qty.to_integral_value() and qty > 1 else qty
    window = line["return_window_days"]
    late = rng.random() < settings.late_request_share
    delay = rng.randint(window + 1, window + 10) if late else rng.randint(1, max(1, window))
    return LinePlan(line["invoice_line_item_id"], line["delivered_on"] + timedelta(days=delay), reason, qty, late)


def _delivered_lines(day: DayContext, start: date, end: date) -> list[dict[str, Any]]:
    """Invoice lines delivered (or handed over) between two dates, with what the return plan needs."""
    columns = """il.invoice_line_item_id, il.order_line_item_id, il.quantity, il.product_id, i.invoice_id, i.order_id,
               {delivered} AS delivered_on, k.customer_type, pk.industry_pack_code AS pack_code,
               p.is_returnable, p.has_expiry, c.return_window_days"""
    joins = """JOIN customers k ON k.customer_id = i.customer_id JOIN products p ON p.product_id = il.product_id
        JOIN product_categories c ON c.product_category_id = p.product_category_id
        JOIN business_units pk ON pk.business_unit_id = p.business_unit_id"""
    return day.rows(f"""
        SELECT {columns.format(delivered="DATE(s.delivered_at)")}
        FROM shipments s JOIN invoices i ON i.shipment_id = s.shipment_id
        JOIN invoice_line_items il ON il.invoice_id = i.invoice_id {joins}
        WHERE s.shipment_status = 'DELIVERED' AND s.delivered_at >= :a AND s.delivered_at < :b
          AND il.line_type = 'PRODUCT' AND i.invoice_status = 'ISSUED'
        UNION ALL
        SELECT {columns.format(delivered="i.invoice_date")}
        FROM invoices i JOIN invoice_line_items il ON il.invoice_id = i.invoice_id {joins}
        WHERE i.shipment_id IS NULL AND i.invoice_date >= :a AND i.invoice_date < :b
          AND il.line_type = 'PRODUCT' AND i.invoice_status = 'ISSUED'
        ORDER BY invoice_line_item_id""", a=start, b=end)


def requests_today(day: DayContext) -> list[tuple[dict[str, Any], LinePlan]]:
    """The planned requests that fall today (plans of lines delivered in the look-back window, kept per run)."""
    refs = day.world.refs
    cache = refs.get("return_plans")
    if cache is None or cache["through"] < day.day - timedelta(days=1):
        start = day.day - timedelta(days=LOOKBACK_DAYS) if cache is None else cache["through"] + timedelta(days=1)
        cache = cache or {"through": start - timedelta(days=1), "by_day": defaultdict(list)}
        for line in _delivered_lines(day, start, day.day):
            plan = _plan(day, line)
            if plan is not None and plan.request_day >= day.day:
                cache["by_day"][plan.request_day].append((line, plan))
        cache["through"] = day.day - timedelta(days=1)
        refs["return_plans"] = cache
    for old in [d for d in cache["by_day"] if d < day.day]:
        del cache["by_day"][old]
    return sorted(cache["by_day"].pop(day.day, []), key=lambda e: e[1].invoice_line_item_id)


# ------------------------------------------------------------------------------------------ requests and approval


def _return_warehouse(day: DayContext, order: dict[str, Any], rng: random.Random) -> tuple[str, int]:
    """Store sales come back to their store (drop-off); shipped orders are collected by a reverse-pickup carrier
    and go back to the warehouse that shipped them (a store may not stock the product)."""
    if order["store_warehouse_id"]:
        return "DROP_AT_STORE", order["store_warehouse_id"]
    return "CARRIER_PICKUP", order["shipment_warehouse_id"]


def create_requests(day: DayContext) -> None:
    plans = requests_today(day)
    if not plans:
        return
    by_order: dict[int, list[tuple[dict[str, Any], LinePlan]]] = defaultdict(list)
    for line, plan in plans:
        by_order[line["order_id"]].append((line, plan))
    orders = {r["order_id"]: r for r in day.rows(f"""
        SELECT o.order_id, o.customer_id, o.store_warehouse_id, k.customer_type, a.city AS shipping_city,
               (SELECT MIN(s.warehouse_id) FROM shipments s WHERE s.order_id = o.order_id
                  AND s.shipment_status = 'DELIVERED') AS shipment_warehouse_id,
               g.company_gst_registration_id, gs.state_code
        FROM orders o JOIN customers k ON k.customer_id = o.customer_id
        LEFT JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
        JOIN invoices i ON i.order_id = o.order_id AND i.invoice_id = (SELECT MIN(invoice_id) FROM invoices x
                                                                     WHERE x.order_id = o.order_id)
        JOIN company_gst_registrations g ON g.company_gst_registration_id = i.company_gst_registration_id
        JOIN states gs ON gs.state_id = g.state_id
        WHERE o.order_id IN ({", ".join(map(str, sorted(by_order)))})""")}
    order_lines = {r["order_line_item_id"]: r for r in day.rows(f"""
        SELECT order_line_item_id, quantity_delivered, quantity_return_requested FROM order_line_items
        WHERE order_id IN ({", ".join(map(str, sorted(by_order)))})""")}
    carriers = [r["shipping_carrier_id"] for r in day.rows(
        "SELECT shipping_carrier_id FROM shipping_carriers WHERE supports_reverse_pickup AND is_active "
        "ORDER BY shipping_carrier_id")]
    agents = staff(day, "CS", ("STAFF", "LEAD", "MANAGER"), day.day) or [approver(day, Decimal(0), ("CS",), day.day)]
    for order_id, entries in sorted(by_order.items()):
        order = orders[order_id]
        rng = day.rng(f"returns.request.{order_id}")
        requested = day.at(rng.randint(9, 20), rng.randint(0, 59), rng.randint(0, 59))
        decided = min(requested + timedelta(minutes=rng.randint(30, 240)), datetime.combine(day.day, time(23, 0)))
        rejection = None
        if any(plan.late for _, plan in entries):
            rejection = "OUTSIDE_RETURN_WINDOW"
        elif any(not line["is_returnable"] for line, _ in entries):
            rejection = "NON_RETURNABLE_PRODUCT"
        lines = []
        for line, plan in entries:
            counter = order_lines[line["order_line_item_id"]]
            free = counter["quantity_delivered"] - counter["quantity_return_requested"]
            qty = min(plan.quantity, free)
            if qty > 0:
                lines.append((line, plan, qty))
                if rejection is None:
                    counter["quantity_return_requested"] += qty
        if not lines:
            continue
        method, warehouse = _return_warehouse(day, order, rng)
        return_id = day.ids.next("sales_returns")
        status = "REJECTED" if rejection else ("PICKUP_SCHEDULED" if method == "CARRIER_PICKUP" else "APPROVED")
        agent = rng.choice(agents)
        carrier = rng.choice(carriers) if method == "CARRIER_PICKUP" else None
        day.writer.insert("sales_returns", {
            "sales_return_id": return_id,
            "return_number": day.numbers.next(day.conn, "RMA", day.day, Registration(
                order["company_gst_registration_id"], order["state_code"])),
            "order_id": order_id, "requested_at": requested, "return_method": method,
            "return_warehouse_id": warehouse, "shipping_carrier_id": carrier,
            "reverse_tracking_number": f"R{return_id:011d}" if carrier else None,
            "return_status": status, "approved_by_employee_id": None if rejection else agent,
            "approved_at": None if rejection else decided, "rejection_reason": rejection, "received_at": None,
            "closed_at": None, "created_at": requested, "updated_at": decided,
        })
        for number, (line, plan, qty) in enumerate(lines, 1):
            day.writer.insert("sales_return_line_items", {
                "sales_return_line_item_id": day.ids.next("sales_return_line_items"), "sales_return_id": return_id,
                "line_number": number, "order_line_item_id": line["order_line_item_id"],
                "invoice_line_item_id": line["invoice_line_item_id"], "return_reason": plan.reason,
                "quantity_requested": qty, "quantity_approved": Decimal(0) if rejection else qty,
                "quantity_received": Decimal(0), "received_at": None, "receiving_location_id": None,
                "line_status": "REJECTED" if rejection else "APPROVED", "created_at": requested, "updated_at": decided,
            })
            if not rejection:
                day.writer.update("order_line_items", "order_line_item_id", line["order_line_item_id"], {
                    "quantity_return_requested": order_lines[line["order_line_item_id"]]["quantity_return_requested"],
                    "updated_at": decided})


# ------------------------------------------------------------------------------------------ goods coming back


def journey(day: DayContext, ret: dict[str, Any]) -> dict[str, datetime]:
    """When the goods are collected and when they arrive (drawn once per return)."""
    rng = day.streams.stream(ret["requested_at"].date(), f"returns.journey.{ret['sales_return_id']}")
    approved = ret["approved_at"]
    if ret["return_method"] == "DROP_AT_STORE":
        dropped = approved.date() + timedelta(days=rng.randint(1, 5))
        return {"received": datetime.combine(dropped, time(rng.randint(11, 19), rng.randint(0, 59)))}
    picked = datetime.combine(approved.date() + timedelta(days=rng.randint(1, 2)),
                              time(rng.randint(10, 17), rng.randint(0, 59)))
    origin, destination = _place(day, ret["customer_city"] or ""), _place(day, ret["warehouse_city"])
    km = _distance(origin, destination) if origin and destination else 500.0
    arrives = picked.date() + timedelta(days=max(1, transit_days(day, "EXPRESS_COURIER", km)))
    if arrives.weekday() == 6:
        arrives += timedelta(days=1)
    return {"picked": picked, "received": datetime.combine(arrives, time(rng.randint(10, 17), rng.randint(0, 59)))}


def open_returns(day: DayContext, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    return day.rows(f"""
        SELECT r.sales_return_id, r.order_id, r.requested_at, r.approved_at, r.return_method, r.return_warehouse_id,
               r.return_status, r.updated_at, a.city AS customer_city, w.city AS warehouse_city,
               w.warehouse_type, o.customer_id
        FROM sales_returns r JOIN orders o ON o.order_id = r.order_id JOIN warehouses w ON w.warehouse_id = r.return_warehouse_id
        LEFT JOIN customer_addresses a ON a.customer_address_id = o.shipping_address_id
        WHERE r.return_status IN ({", ".join(f"'{s}'" for s in statuses)}) ORDER BY r.sales_return_id""")


def pickups(day: DayContext) -> None:
    """Reverse pickups collected today (PICKUP_SCHEDULED → IN_TRANSIT)."""
    for ret in open_returns(day, ("PICKUP_SCHEDULED",)):
        plan = journey(day, ret)
        if plan["picked"].date() == day.day:
            day.writer.update("sales_returns", "sales_return_id", ret["sales_return_id"],
                              {"return_status": "IN_TRANSIT", "updated_at": plan["picked"]})


def receipts_due(day: DayContext) -> list[tuple[datetime, dict[str, Any]]]:
    """Returns whose goods arrive today (for the fulfilment step's time-ordered stock pass)."""
    due = []
    for ret in open_returns(day, ("APPROVED", "IN_TRANSIT")):
        plan = journey(day, ret)
        if plan["received"].date() == day.day:
            due.append((plan["received"], ret))
        elif plan["received"].date() < day.day:
            raise RuntimeError(f"Return {ret['sales_return_id']} should have arrived on {plan['received'].date()}")
    return due


def issue_of_invoice_line(column: str, line: str = "il") -> str:
    """SQL: ``column`` of the first sales issue of invoice line ``line`` (a shipped line's issue is on its shipment
    line, D48; a handover's on the invoice line). Two indexed lookups, not an OR, which would scan the ledger."""
    issue = ("SELECT {c} FROM inventory_transactions t WHERE t.{key} = {line}.{key} AND t.transaction_type = 'SALES_ISSUE' "
             "AND t.reversal_of_inventory_transaction_id IS NULL ORDER BY t.inventory_transaction_id LIMIT 1")
    return (f"COALESCE(({issue.format(c=column, key='shipment_line_item_id', line=line)}), "
            f"({issue.format(c=column, key='invoice_line_item_id', line=line)}))")


def receipt_lines(day: DayContext, return_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Return lines with the original issue of their invoice line (cost, batch) and the product's business unit."""
    found: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not return_ids:
        return found
    for row in day.rows(f"""
            SELECT rl.sales_return_line_item_id, rl.sales_return_id, rl.order_line_item_id, rl.quantity_approved,
                   il.product_id, p.business_unit_id,
                   {issue_of_invoice_line("t.unit_cost")} AS unit_cost,
                   {issue_of_invoice_line("t.inventory_batch_id")} AS inventory_batch_id
            FROM sales_return_line_items rl JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
            JOIN products p ON p.product_id = il.product_id
            WHERE rl.sales_return_id IN ({", ".join(map(str, sorted(return_ids)))}) AND rl.quantity_approved > 0
            ORDER BY rl.sales_return_id, rl.line_number"""):
        found[row["sales_return_id"]].append(row)
    return found


def quarantine_location(day: DayContext, warehouse_id: int) -> int:
    cache = day.world.refs.setdefault("returns_locations", {})
    if warehouse_id not in cache:
        rows = day.rows("SELECT warehouse_location_id FROM warehouse_locations WHERE warehouse_id = :w "
                        "AND zone = 'RETURNS' AND is_active ORDER BY warehouse_location_id", w=warehouse_id)
        cache[warehouse_id] = rows[0]["warehouse_location_id"]
    return cache[warehouse_id]




def receive(day: DayContext, invoicer, stock, ret: dict[str, Any], lines: list[dict[str, Any]], at: datetime) -> None:
    """The goods are back: QUARANTINE at the original issue cost (RETURN_RECEIPT), P12 Dr Inventory / Cr COGS."""
    warehouse = ret["return_warehouse_id"]
    location = quarantine_location(day, warehouse)
    cogs: dict[int, Decimal] = defaultdict(Decimal)
    for line in lines:
        qty = line["quantity_approved"]
        value = money(product(qty, line["unit_cost"]))
        day.writer.update("sales_return_line_items", "sales_return_line_item_id", line["sales_return_line_item_id"], {
            "quantity_received": qty, "received_at": at, "receiving_location_id": location, "line_status": "RECEIVED",
            "updated_at": at})
        day.writer.insert("inventory_transactions", {
            "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
            "warehouse_id": warehouse, "product_id": line["product_id"], "inventory_batch_id": line["inventory_batch_id"],
            "warehouse_location_id": location, "transaction_type": "RETURN_RECEIPT", "stock_status": "QUARANTINE",
            "quantity": qty, "unit_cost": line["unit_cost"], "value_change": value, "goods_receipt_line_item_id": None,
            "shipment_line_item_id": None, "invoice_line_item_id": None,
            "sales_return_line_item_id": line["sales_return_line_item_id"], "return_inspection_id": None,
            "stock_transfer_line_item_id": None, "stock_adjustment_line_item_id": None, "defective_stock_id": None,
            "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
            "created_at": at, "updated_at": at,
        })
        stock.back_in(warehouse, line["product_id"], qty, value, at, receipt=True, bucket="qty_quarantine")
        cogs[line["business_unit_id"]] += value
    day.writer.update("sales_returns", "sales_return_id", ret["sales_return_id"],
                      {"return_status": "RECEIVED", "received_at": at, "updated_at": at})
    book = invoicer.book
    builder = JournalBuilder(f"Return received {ret['sales_return_id']}")
    builder.debit(book.role("INVENTORY"), sum(cogs.values(), Decimal(0)), warehouse_id=warehouse)
    for unit in sorted(cogs):
        builder.credit(book.cogs(unit), cogs[unit], cost_center_id=invoicer.bu_cc[unit])
    post_journal(day, builder.build(), source="SALES_RETURN_RECEIPT", sales_return_id=ret["sales_return_id"])


# ------------------------------------------------------------------------------------------ inspection and credit

SELLABLE_ODDS = {"SIZE_OR_FIT": 0.92, "CHANGED_MIND": 0.90, "WRONG_ITEM": 0.90, "NOT_AS_DESCRIBED": 0.70,
                 "NEAR_EXPIRY": 0.20, "DAMAGED_IN_TRANSIT": 0.15, "DEFECTIVE": 0.10, "WARRANTY_CLAIM": 0.15}


def inspections(day: DayContext, invoicer) -> None:
    """Received return lines are inspected 1–3 days after receipt (a further inspection 1–3 days later)."""
    if not day.is_working_day:
        return
    settings = day.world.config.returns
    lines = day.rows("""
        SELECT rl.sales_return_line_item_id, rl.sales_return_id, rl.return_reason, rl.quantity_received, rl.received_at,
               rl.receiving_location_id, r.return_warehouse_id, il.product_id, t.unit_cost, t.inventory_batch_id,
               COALESCE(x.inspections, 0) AS inspections, x.last_at, x.further, w.company_gst_registration_id,
               gs.state_code
        FROM sales_return_line_items rl JOIN sales_returns r ON r.sales_return_id = rl.sales_return_id
        JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
        JOIN inventory_transactions t ON t.sales_return_line_item_id = rl.sales_return_line_item_id
             AND t.transaction_type = 'RETURN_RECEIPT'
        JOIN warehouses w ON w.warehouse_id = r.return_warehouse_id
        JOIN company_gst_registrations g ON g.company_gst_registration_id = w.company_gst_registration_id
        JOIN states gs ON gs.state_id = g.state_id
        LEFT JOIN (SELECT sales_return_line_item_id, COUNT(*) AS inspections, MAX(inspected_at) AS last_at,
                          SUM(quantity_further_inspection) - SUM(IF(inspection_sequence > 1, quantity_inspected, 0)) AS further
                   FROM return_inspections GROUP BY sales_return_line_item_id) x
               ON x.sales_return_line_item_id = rl.sales_return_line_item_id
        WHERE rl.line_status = 'RECEIVED' ORDER BY rl.sales_return_line_item_id""")
    if not lines:
        return
    inspectors = _inspectors(day, sorted({l["return_warehouse_id"] for l in lines}))
    moves: dict[tuple[int, int], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    touched: dict[tuple[int, int], datetime] = {}
    for line in lines:
        number = line["inspections"] + 1
        rng = day.streams.stream(line["received_at"].date(), f"returns.inspect.{line['sales_return_line_item_id']}.{number}")
        since = line["last_at"] or line["received_at"]
        if day.day < since.date() + timedelta(days=rng.randint(1, 3)):
            continue
        qty = line["quantity_received"] if number == 1 else line["further"]
        at = day.at(rng.randint(10, 17), rng.randint(0, 59))
        further = Decimal(0)
        if number == 1 and rng.random() < settings.further_inspection_share:
            further = qty
            sellable = defective = Decimal(0)
        else:
            good = rng.random() < SELLABLE_ODDS[line["return_reason"]]
            sellable, defective = (qty, Decimal(0)) if good else (Decimal(0), qty)
        result = "FURTHER_INSPECTION" if further else ("PASSED" if sellable else "DEFECTIVE")
        inspection_id = day.ids.next("return_inspections")
        by = rng.choice(inspectors[line["return_warehouse_id"]])
        day.writer.insert("return_inspections", {
            "return_inspection_id": inspection_id, "sales_return_line_item_id": line["sales_return_line_item_id"],
            "inspection_sequence": number, "inspected_at": at, "inspected_by_employee_id": by,
            "quantity_inspected": qty, "quantity_sellable": sellable, "quantity_defective": defective,
            "quantity_further_inspection": further, "inspection_result": result,
            "defect_reason": DEFECTS[line["return_reason"]] if defective else None,
            "inspection_notes": "Needs a second look" if further else None, "created_at": at, "updated_at": at,
        })
        key = (line["return_warehouse_id"], line["product_id"])
        for bucket, moved in (("qty_sellable", sellable), ("qty_defective", defective)):
            if moved:
                for status, signed in (("QUARANTINE", -moved), ("SELLABLE" if bucket == "qty_sellable" else "DEFECTIVE",
                                                                moved)):
                    day.writer.insert("inventory_transactions", {
                        "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
                        "warehouse_id": line["return_warehouse_id"], "product_id": line["product_id"],
                        "inventory_batch_id": line["inventory_batch_id"],
                        "warehouse_location_id": line["receiving_location_id"], "transaction_type": "RETURN_DISPOSITION",
                        "stock_status": status, "quantity": signed, "unit_cost": line["unit_cost"],
                        "value_change": Decimal(0), "goods_receipt_line_item_id": None, "shipment_line_item_id": None,
                        "invoice_line_item_id": None, "sales_return_line_item_id": None,
                        "return_inspection_id": inspection_id, "stock_transfer_line_item_id": None,
                        "stock_adjustment_line_item_id": None, "defective_stock_id": None,
                        "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
                        "created_at": at, "updated_at": at,
                    })
                moves[key]["qty_quarantine"] -= moved
                moves[key][bucket] += moved
                touched[key] = max(at, touched.get(key, at))
        if defective:
            day.writer.insert("defective_stock", {
                "defective_stock_id": day.ids.next("defective_stock"),
                "defect_number": day.numbers.next(day.conn, "DEF", day.day, Registration(
                    line["company_gst_registration_id"], line["state_code"])),
                "warehouse_id": line["return_warehouse_id"], "product_id": line["product_id"],
                "inventory_batch_id": line["inventory_batch_id"], "quantity": defective,
                "return_inspection_id": inspection_id, "stock_adjustment_line_item_id": None,
                "stock_transfer_line_item_id": None, "defect_reason": DEFECTS[line["return_reason"]],
                "detected_at": at, "disposition": "PENDING", "disposition_status": "PENDING", "disposed_at": None,
                "disposed_by_employee_id": None, "supplier_id": None, "repair_cost_amount": None,
                "created_at": at, "updated_at": at,
            })
        if not further:
            day.writer.update("sales_return_line_items", "sales_return_line_item_id", line["sales_return_line_item_id"],
                              {"line_status": "INSPECTED", "updated_at": at})
    if moves:
        day.execute("""UPDATE inventory SET qty_quarantine = qty_quarantine + :q, qty_sellable = qty_sellable + :s,
                              qty_defective = qty_defective + :d, updated_at = GREATEST(updated_at, :t)
                       WHERE warehouse_id = :w AND product_id = :p""",
                    [{"q": m["qty_quarantine"], "s": m["qty_sellable"], "d": m["qty_defective"], "t": touched[key],
                      "w": key[0], "p": key[1]} for key, m in sorted(moves.items())])


def _inspectors(day: DayContext, warehouses: list[int]) -> dict[int, list[int]]:
    found: dict[int, list[int]] = {w: [] for w in warehouses}
    for row in day.rows(f"""
            SELECT employee_id, warehouse_id FROM employees WHERE warehouse_id IN ({", ".join(map(str, warehouses))})
              AND employment_status = 'ACTIVE' AND hire_date <= :d AND job_level IN ('LEAD', 'MANAGER', 'STAFF')
            ORDER BY employee_id""", d=day.day):
        found[row["warehouse_id"]].append(row["employee_id"])
    return found


def credit_inspected(day: DayContext, invoicer) -> None:
    """D44: once every line is inspected, one credit note per invoice for all received quantity (P11)."""
    ready = day.rows("""
        SELECT r.sales_return_id, r.order_id, r.updated_at FROM sales_returns r
        WHERE r.return_status = 'RECEIVED' AND NOT EXISTS (
            SELECT 1 FROM sales_return_line_items l WHERE l.sales_return_id = r.sales_return_id
              AND l.quantity_approved > 0 AND l.line_status <> 'INSPECTED')
        ORDER BY r.sales_return_id""")
    if not ready:
        return
    ids = ", ".join(str(r["sales_return_id"]) for r in ready)
    lines = day.rows(f"""
        SELECT rl.sales_return_line_item_id, rl.sales_return_id, rl.invoice_line_item_id, rl.quantity_received,
               il.invoice_id, il.quantity AS invoiced, il.unit_price, il.inclusive_unit_price, il.discount_percent,
               il.gst_rate, il.taxable_amount AS invoiced_taxable,
               COALESCE((SELECT SUM(cl.taxable_amount) FROM credit_note_line_items cl
                         JOIN credit_notes n ON n.credit_note_id = cl.credit_note_id
                         WHERE cl.invoice_line_item_id = il.invoice_line_item_id AND n.credit_note_status = 'ISSUED'), 0)
                   AS credited_taxable,
               (SELECT MAX(i2.inspected_at) FROM return_inspections i2
                WHERE i2.sales_return_line_item_id = rl.sales_return_line_item_id) AS inspected_at
        FROM sales_return_line_items rl JOIN invoice_line_items il ON il.invoice_line_item_id = rl.invoice_line_item_id
        WHERE rl.sales_return_id IN ({ids}) AND rl.quantity_received > 0
        ORDER BY rl.sales_return_id, il.invoice_id, rl.line_number""")
    invoices = load_invoices(day, sorted({l["invoice_id"] for l in lines}))
    for ret in ready:
        mine = [l for l in lines if l["sales_return_id"] == ret["sales_return_id"]]
        at = max(l["inspected_at"] for l in mine) + timedelta(minutes=30)
        for invoice_id in sorted({l["invoice_id"] for l in mine}):
            credited: dict[int, dict[str, Any]] = {}
            for line in (l for l in mine if l["invoice_id"] == invoice_id):
                if line["inclusive_unit_price"] is not None:
                    priced = price_inclusive(line["quantity_received"], line["inclusive_unit_price"],
                                             line["discount_percent"], line["gst_rate"])
                else:
                    priced = price_exclusive(line["quantity_received"], line["unit_price"], line["discount_percent"],
                                             line["gst_rate"])
                taxable = min(priced.taxable_amount, line["invoiced_taxable"] - line["credited_taxable"])
                credited[line["invoice_line_item_id"]] = {
                    "quantity": line["quantity_received"], "unit_price": priced.unit_price, "taxable_amount": taxable,
                    "sales_return_line_item_id": line["sales_return_line_item_id"]}
            invoice = invoices[invoice_id]
            approved_by = approver(day, invoice["total_amount"], ("FIN", "CS"), day.day)
            issue_credit_note(day, invoicer, invoice, "SALES_RETURN", at, approved_by, credited=credited,
                              sales_return_id=ret["sales_return_id"])
        for line in mine:
            day.writer.update("sales_return_line_items", "sales_return_line_item_id", line["sales_return_line_item_id"],
                              {"line_status": "CREDITED", "updated_at": at})
        day.writer.update("sales_returns", "sales_return_id", ret["sales_return_id"],
                          {"return_status": "INSPECTED", "updated_at": at})


def close_returns(day: DayContext) -> None:
    """A credited return is CLOSED once nothing is owed to the customer on it any more (refunds: step 3.11b)."""
    for ret in day.rows("""
            SELECT r.sales_return_id, MAX(a.updated_at) AS settled_at FROM sales_returns r
            JOIN credit_notes n ON n.sales_return_id = r.sales_return_id
            JOIN accounts_receivable a ON a.credit_note_id = n.credit_note_id
            WHERE r.return_status = 'INSPECTED' GROUP BY r.sales_return_id
            HAVING SUM(a.open_amount) = 0 ORDER BY r.sales_return_id"""):
        at = max(ret["settled_at"], day.at(0))
        day.writer.update("sales_returns", "sales_return_id", ret["sales_return_id"],
                          {"return_status": "CLOSED", "closed_at": at, "updated_at": at})


def close_orders(day: DayContext) -> None:
    """Delivered orders whose return windows are over, with no return in progress, are CLOSED."""
    refs = day.world.refs
    if "max_return_window" not in refs:
        refs["max_return_window"] = day.rows("SELECT MAX(return_window_days) AS w FROM product_categories")[0]["w"] or 30
    cutoff = day.at(0) - timedelta(days=refs["max_return_window"] + 11)
    orders = day.rows("""
        SELECT o.order_id, o.updated_at FROM orders o
        WHERE o.order_status = 'DELIVERED' AND o.updated_at < :c
          AND NOT EXISTS (SELECT 1 FROM sales_returns r WHERE r.order_id = o.order_id
                            AND r.return_status NOT IN ('CLOSED', 'REJECTED', 'CANCELLED'))
        ORDER BY o.order_id""", c=cutoff)
    if not orders:
        return
    at = day.at(23, 30)
    day.execute("UPDATE order_line_items SET line_status = 'CLOSED', updated_at = :t WHERE order_id = :o "
                "AND line_status = 'DELIVERED'", [{"t": at, "o": o["order_id"]} for o in orders])
    day.execute("UPDATE orders SET order_status = 'CLOSED', updated_at = :t WHERE order_id = :o",
                [{"t": at, "o": o["order_id"]} for o in orders])
    for order in orders:
        record_status(day, "order", order["order_id"], "DELIVERED", "CLOSED", at, None, "Return window over")


def returns_step(day: DayContext) -> None:
    """Day step 8 (1G §2.2): new requests and approvals, pickups, inspections, credit notes, refunds, closing."""
    from scmgen.generation.engine.fulfilment.invoicing import Invoicer
    from scmgen.generation.engine.refunds import refunds_step
    create_requests(day)
    pickups(day)
    invoicer = Invoicer(day)
    inspections(day, invoicer)
    credit_inspected(day, invoicer)
    refunds_step(day)
    close_returns(day)
    close_orders(day)
