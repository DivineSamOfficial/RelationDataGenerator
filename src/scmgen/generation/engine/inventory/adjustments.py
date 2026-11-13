"""Stock adjustments and defective dispositions (1C §5 ``stock_adjustments``; 1D ``defective_stock``; D45, D60;
1E P15, P18; step 3.12b).

Morning, before anything else moves (``morning_stock_step``):

* **Expiry sweep** (daily, 06:00–06:29): sellable stock of batches whose expiry date has come is written off in one
  ``EXPIRY_WRITE_OFF`` adjustment per warehouse (reason EXPIRED, at MAC, P15 Dr Inventory Write-offs / Cr
  Inventory). Only the unreserved part can go (the database keeps reserved ≤ sellable): an expired reserved
  remainder is released by the dispatch or the till and swept the next morning.
* **Cycle counts** (monthly, first working day on or after ``inventory.cycle_count_day``, 06:30–06:59): a rotating
  ABC sample per warehouse by stock value (A items monthly, B in quarter-end months, C in March and September).
  Most counts agree; ``count_difference_share`` of them find a difference: shrinkage (THEFT_OR_LOSS, at MAC,
  never into reserved stock) or found stock (FOUND, at MAC). One ``CYCLE_COUNT`` adjustment per warehouse and day
  with a line per difference (book and counted quantity); every counted row gets ``last_counted_at``.

Evening, after the day's picking and before transfers leave (``evening_stock_step``), each event after the last
movement of its product in that warehouse (moving-average replay, V-IN-09):

* **Handling damage** (``handling_damage_per_month`` per warehouse type, Poisson per working day): a ``DAMAGE``
  adjustment reclassifying 1–3 units SELLABLE → DEFECTIVE (two lines, no value change) with a HANDLING_DAMAGE
  ``defective_stock`` entry on the DEFECTIVE line.
* **Defective dispositions** (weekly on ``disposition_weekday``, entries at least ``disposition_min_days`` old):
  the warehouse manager decides by the product's industry pack (``returns.dispositions``): SCRAP or
  RETURN_TO_SUPPLIER (the preferred supplier; scrapped when there is none) write the units off
  (DEFECTIVE_DISPOSITION at MAC, P18); REPAIR moves them back to SELLABLE (value unchanged) with the repair cost as
  a memo (D60).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO, money, product
from scmgen.domain.costing import Movement, StockPosition
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext, poisson
from scmgen.generation.engine.documents import approver
from scmgen.generation.engine.inventory.transfers import _crew, defective_location, shelf_location, warehouses
from scmgen.generation.engine.sales.allocation import expired_stock
from scmgen.generation.numbering import Registration

STATUS_COLUMN = {"SELLABLE": "qty_sellable", "QUARANTINE": "qty_quarantine", "DEFECTIVE": "qty_defective"}


# ------------------------------------------------------------------------------------------ positions


class StockBook:
    """Positions (quantity per status, value, MAC) and batch balances of the warehouse × product pairs touched."""

    def __init__(self, day: DayContext, pairs: set[tuple[int, int]]) -> None:
        self.day = day
        self.rows: dict[tuple[int, int], dict[str, Any]] = {}
        self.batches: dict[tuple[int, int, str], dict[int, dict[str, Any]]] = defaultdict(dict)
        self.changes: dict[tuple[int, int], dict[str, Any]] = {}
        if not pairs:
            return
        in_pairs = ", ".join(f"({w}, {p})" for w, p in sorted(pairs))
        for row in day.rows(f"""
                SELECT i.inventory_id, i.warehouse_id, i.product_id, i.qty_sellable, i.qty_quarantine,
                       i.qty_defective, i.qty_reserved, i.stock_value, i.last_unit_cost, i.last_receipt_at,
                       i.last_issue_at, p.is_batch_tracked, p.business_unit_id
                FROM inventory i JOIN products p ON p.product_id = i.product_id
                WHERE (i.warehouse_id, i.product_id) IN ({in_pairs})"""):
            on_hand = row["qty_sellable"] + row["qty_quarantine"] + row["qty_defective"]
            self.rows[(row["warehouse_id"], row["product_id"])] = {
                **row, "position": StockPosition(on_hand, row["stock_value"], row["last_unit_cost"])}
        tracked = sorted({k for k, r in self.rows.items() if r["is_batch_tracked"]})
        if tracked:
            for row in day.rows(f"""
                    SELECT t.warehouse_id, t.product_id, t.stock_status, t.inventory_batch_id, SUM(t.quantity) AS qty,
                           b.expiry_date
                    FROM inventory_transactions t JOIN inventory_batches b ON b.inventory_batch_id = t.inventory_batch_id
                    WHERE (t.warehouse_id, t.product_id) IN ({", ".join(f"({w}, {p})" for w, p in tracked)})
                      AND t.stock_status IN ('SELLABLE', 'DEFECTIVE')
                    GROUP BY t.warehouse_id, t.product_id, t.stock_status, t.inventory_batch_id, b.expiry_date
                    HAVING SUM(t.quantity) > 0"""):
                self.batches[(row["warehouse_id"], row["product_id"], row["stock_status"])][row["inventory_batch_id"]] = {
                    "qty": row["qty"], "expiry": row["expiry_date"]}

    def tracked(self, key: tuple[int, int]) -> bool:
        return bool(self.rows[key]["is_batch_tracked"])

    def last_moved(self, key: tuple[int, int]) -> datetime | None:
        row = self.rows[key]
        change = self.changes.get(key)
        times = [t for t in (row["last_receipt_at"], row["last_issue_at"], change and change["at"]) if t is not None]
        return max(times, default=None)

    def free(self, key: tuple[int, int], on: date) -> Decimal:
        """Sellable, unreserved, unexpired quantity (what a write-off or damage may take without touching
        reservations)."""
        row = self.rows[key]
        free = row["qty_sellable"] - row["qty_reserved"]
        if self.tracked(key):
            fresh = sum((b["qty"] for b in self.batches[(*key, "SELLABLE")].values()
                         if b["expiry"] is None or b["expiry"] > on), ZERO)
            free = min(free, fresh)
        return max(free, ZERO)

    def largest_batch(self, key: tuple[int, int], status: str, on: date) -> int | None:
        fresh = [(b["qty"], batch_id) for batch_id, b in self.batches[(*key, status)].items()
                 if b["qty"] > 0 and (b["expiry"] is None or b["expiry"] > on)]
        return max(fresh)[1] if fresh else None

    def batch_qty(self, key: tuple[int, int], status: str, batch_id: int | None) -> Decimal:
        if batch_id is None:
            return self.rows[key][STATUS_COLUMN[status]]
        return self.batches[(*key, status)].get(batch_id, {"qty": ZERO})["qty"]

    def _touch(self, key: tuple[int, int], status: str, qty: Decimal, at: datetime, batch_id: int | None,
               direction: str) -> None:
        row = self.rows[key]
        row[STATUS_COLUMN[status]] += qty
        if batch_id is not None:
            batch = self.batches[(*key, status)].setdefault(batch_id, {"qty": ZERO, "expiry": None})
            batch["qty"] += qty
        change = self.changes.setdefault(key, {"qty_sellable": ZERO, "qty_quarantine": ZERO, "qty_defective": ZERO,
                                               "at": at, "out": None, "in": None})
        change[STATUS_COLUMN[status]] += qty
        change["at"] = max(change["at"], at)
        change[direction] = max(change[direction] or at, at)

    def take(self, key: tuple[int, int], qty: Decimal, status: str, at: datetime, batch_id: int | None) -> Movement:
        """Stock out of a bucket at MAC (write-off)."""
        row = self.rows[key]
        row["position"], movement = row["position"].issue(qty)
        self._touch(key, status, -qty, at, batch_id, "out")
        return movement

    def add(self, key: tuple[int, int], qty: Decimal, status: str, at: datetime, batch_id: int | None,
            reference_cost: Decimal) -> Movement:
        """Found stock into a bucket at MAC (or the last / reference cost when the position is empty)."""
        row = self.rows[key]
        row["position"], movement = row["position"].receive_found(qty, reference_cost)
        self._touch(key, status, qty, at, batch_id, "in")
        return movement

    def move(self, key: tuple[int, int], qty: Decimal, source: str, target: str, at: datetime,
             batch_id: int | None) -> Decimal:
        """Reclassify between buckets: quantity moves, value and MAC don't. Returns the MAC for the rows."""
        self._touch(key, source, -qty, at, batch_id, "out")
        self._touch(key, target, qty, at, batch_id, "in")
        return self.rows[key]["position"].moving_average_cost or ZERO

    def write(self, counted: dict[tuple[int, int], datetime] | None = None) -> None:
        rows = []
        for key, change in sorted(self.changes.items()):
            row = self.rows[key]
            rows.append({"i": row["inventory_id"], "s": change["qty_sellable"], "q": change["qty_quarantine"],
                         "d": change["qty_defective"], "v": row["position"].value, "t": change["at"],
                         "o": change["out"], "n": change["in"]})
        if rows:
            self.day.execute("""
                UPDATE inventory SET qty_sellable = qty_sellable + :s, qty_quarantine = qty_quarantine + :q,
                       qty_defective = qty_defective + :d, stock_value = :v,
                       last_issue_at = IF(:o IS NULL, last_issue_at, GREATEST(COALESCE(last_issue_at, :o), :o)),
                       last_receipt_at = IF(:n IS NULL, last_receipt_at, GREATEST(COALESCE(last_receipt_at, :n), :n)),
                       updated_at = GREATEST(updated_at, :t)
                WHERE inventory_id = :i""", rows)
        if counted:
            self.day.execute("""UPDATE inventory SET last_counted_at = :t, updated_at = GREATEST(updated_at, :t)
                                WHERE warehouse_id = :w AND product_id = :p""",
                             [{"t": t, "w": w, "p": p} for (w, p), t in sorted(counted.items())])
        self.changes.clear()


# ------------------------------------------------------------------------------------------ adjustment documents


@dataclass
class AdjustmentLine:
    product_id: int
    batch_id: int | None
    status: str
    quantity_change: Decimal
    unit_cost: Decimal
    value_change: Decimal
    reason_code: str
    location_id: int
    system_quantity: Decimal | None = None
    counted_quantity: Decimal | None = None
    defect_reason: str | None = None


def post_adjustment(day: DayContext, warehouse: dict[str, Any], kind: str, reason: str, at: datetime,
                    lines: list[AdjustmentLine], rng) -> int | None:
    """One POSTED adjustment with its lines, ledger rows, defective entries and P15 (losses: Dr Inventory
    Write-offs / Cr Inventory; gains: Dr Inventory / Cr Inventory Adjustment Gains; no journal at zero value)."""
    if not lines:
        return None
    losses = -sum((l.value_change for l in lines if l.value_change < 0), ZERO)
    gains = sum((l.value_change for l in lines if l.value_change > 0), ZERO)
    # Queries first (approver, crew, numbers): a query flushes the buffer, and the ledger rows must not reach the
    # database before their adjustment lines.
    approved_by = approver(day, max(losses, gains), ("WHO", "FIN"), day.day)
    created_by = rng.choice(_crew(day, warehouse["warehouse_id"]))
    registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
    number = day.numbers.next(day.conn, "ADJ", day.day, registration)
    defect_numbers = [day.numbers.next(day.conn, "DEF", day.day, registration) for l in lines if l.defect_reason]
    book = account_book(day)
    adjustment_id = day.ids.next("stock_adjustments")
    created = at - timedelta(minutes=rng.randint(5, 25))
    day.writer.insert("stock_adjustments", {
        "stock_adjustment_id": adjustment_id, "adjustment_number": number, "warehouse_id": warehouse["warehouse_id"],
        "adjustment_date": day.day, "adjustment_type": kind, "reason": reason[:255], "adjustment_status": "POSTED",
        "created_by_employee_id": created_by, "approved_by_employee_id": approved_by, "posted_at": at,
        "created_at": created, "updated_at": at,
    })
    for line_number, line in enumerate(lines, 1):
        line_id = day.ids.next("stock_adjustment_line_items")
        day.writer.insert("stock_adjustment_line_items", {
            "stock_adjustment_line_item_id": line_id, "stock_adjustment_id": adjustment_id,
            "line_number": line_number, "product_id": line.product_id, "inventory_batch_id": line.batch_id,
            "stock_status": line.status, "system_quantity": line.system_quantity,
            "counted_quantity": line.counted_quantity, "quantity_change": line.quantity_change,
            "unit_cost": line.unit_cost, "value_change": line.value_change, "reason_code": line.reason_code,
            "created_at": created, "updated_at": at,
        })
        day.writer.insert("inventory_transactions", {
            "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
            "warehouse_id": warehouse["warehouse_id"], "product_id": line.product_id,
            "inventory_batch_id": line.batch_id, "warehouse_location_id": line.location_id,
            "transaction_type": "ADJUSTMENT", "stock_status": line.status, "quantity": line.quantity_change,
            "unit_cost": line.unit_cost, "value_change": line.value_change, "goods_receipt_line_item_id": None,
            "shipment_line_item_id": None, "invoice_line_item_id": None, "sales_return_line_item_id": None,
            "return_inspection_id": None, "stock_transfer_line_item_id": None,
            "stock_adjustment_line_item_id": line_id, "defective_stock_id": None,
            "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
            "created_at": at, "updated_at": at,
        })
        if line.defect_reason:
            day.writer.insert("defective_stock", {
                "defective_stock_id": day.ids.next("defective_stock"), "defect_number": defect_numbers.pop(0),
                "warehouse_id": warehouse["warehouse_id"], "product_id": line.product_id,
                "inventory_batch_id": line.batch_id, "quantity": line.quantity_change, "return_inspection_id": None,
                "stock_adjustment_line_item_id": line_id, "stock_transfer_line_item_id": None,
                "defect_reason": line.defect_reason, "detected_at": at, "disposition": "PENDING",
                "disposition_status": "PENDING", "disposed_at": None, "disposed_by_employee_id": None,
                "supplier_id": None, "repair_cost_amount": None, "created_at": at, "updated_at": at,
            })
    if losses or gains:
        journal = JournalBuilder(f"Stock adjustment {number}")
        if losses:
            journal.debit(book.role("INVENTORY_WRITE_OFF"), losses, cost_center_id=warehouse["cost_center_id"])
            journal.credit(book.role("INVENTORY"), losses, warehouse_id=warehouse["warehouse_id"])
        if gains:
            journal.debit(book.role("INVENTORY"), gains, warehouse_id=warehouse["warehouse_id"])
            journal.credit(book.role("INVENTORY_ADJUSTMENT_GAIN"), gains, cost_center_id=warehouse["cost_center_id"])
        post_journal(day, journal.build(), source="STOCK_ADJUSTMENT", stock_adjustment_id=adjustment_id)
    return adjustment_id


# ------------------------------------------------------------------------------------------ expiry sweep


def expiry_sweep(day: DayContext) -> None:
    """Expired sellable batches are written off first thing in the morning (EXPIRY_WRITE_OFF, P15)."""
    expired = expired_stock(day)
    keys = sorted(k for k, q in expired.items() if q > 0)
    if not keys:
        return
    sites = warehouses(day)
    stock = StockBook(day, set(keys))
    per_warehouse: dict[int, list[AdjustmentLine]] = defaultdict(list)
    rng = day.rng("inventory.expiry")
    at = day.at(6, rng.randint(0, 29), rng.randint(0, 59))
    for key in keys:
        if key not in stock.rows:
            continue
        room = stock.rows[key]["qty_sellable"] - stock.rows[key]["qty_reserved"]
        batches = sorted((b["expiry"], batch_id, b["qty"]) for batch_id, b in stock.batches[(*key, "SELLABLE")].items()
                         if b["qty"] > 0 and b["expiry"] is not None and b["expiry"] <= day.day)
        for _, batch_id, qty in batches:
            qty = min(qty, room)
            if qty <= 0:
                break
            movement = stock.take(key, qty, "SELLABLE", at, batch_id)
            room -= qty
            expired[key] -= qty
            per_warehouse[key[0]].append(AdjustmentLine(
                key[1], batch_id, "SELLABLE", -qty, movement.unit_cost, movement.value_change, "EXPIRED",
                shelf_location(day, sites[key[0]], key[1])))
    for warehouse_id, lines in sorted(per_warehouse.items()):
        post_adjustment(day, sites[warehouse_id], "EXPIRY_WRITE_OFF", "Expired batches written off", at, lines,
                        day.rng(f"inventory.expiry.{warehouse_id}"))
    stock.write()


# ------------------------------------------------------------------------------------------ cycle counts


def _count_day(day: DayContext) -> bool:
    settings = day.world.config.inventory
    first = day.day.replace(day=settings.cycle_count_day)
    if day.day < first:
        return False
    probe = first
    while not day.world.config.daily.is_working_day(probe):
        probe += timedelta(days=1)
    return probe == day.day


def _classes_counted(month: int) -> tuple[str, ...]:
    """A every month, B in quarter-end months, C twice a year (March and September)."""
    return ("A",) + (("B",) if month % 3 == 0 else ()) + (("C",) if month in (3, 9) else ())


def cycle_counts(day: DayContext) -> None:
    if not day.is_working_day or not _count_day(day):
        return
    settings = day.world.config.inventory
    sites = warehouses(day)
    classes = _classes_counted(day.day.month)
    counted: list[tuple[int, int]] = []
    for warehouse_id in sorted(sites):
        rows = day.rows("""SELECT product_id, stock_value FROM inventory
                           WHERE warehouse_id = :w AND qty_sellable > 0 ORDER BY stock_value DESC, product_id""",
                        w=warehouse_id)
        a_end = round(len(rows) * settings.abc_shares[0])
        b_end = a_end + round(len(rows) * settings.abc_shares[1])
        for rank, row in enumerate(rows):
            klass = "A" if rank < a_end else "B" if rank < b_end else "C"
            if klass in classes:
                counted.append((warehouse_id, row["product_id"]))
    if not counted:
        return
    stock = StockBook(day, set(counted))
    references = {r["product_id"]: r["reference_unit_cost"] for r in day.rows(f"""
        SELECT product_id, MIN(reference_unit_cost) AS reference_unit_cost FROM supplier_products
        WHERE product_id IN ({", ".join(sorted({str(p) for _, p in counted}))}) GROUP BY product_id""")}
    per_warehouse: dict[int, list[AdjustmentLine]] = defaultdict(list)
    counted_at: dict[tuple[int, int], datetime] = {}
    times = {w: day.at(6, 30 + day.rng(f"inventory.count.{w}").randint(0, 25)) for w in sites}
    for key in sorted(counted):
        if key not in stock.rows:
            continue
        rng = day.rng(f"inventory.count.{key[0]}.{key[1]}")
        at = times[key[0]] + timedelta(seconds=rng.randint(0, 240))
        counted_at[key] = at
        if rng.random() >= settings.count_difference_share:
            continue
        batch_id = stock.largest_batch(key, "SELLABLE", day.day) if stock.tracked(key) else None
        if stock.tracked(key) and batch_id is None:
            continue
        book_qty = stock.batch_qty(key, "SELLABLE", batch_id)
        size = Decimal(rng.randint(1, max(1, int(book_qty * Decimal(str(settings.max_difference_share))))))
        location = shelf_location(day, sites[key[0]], key[1])
        if rng.random() < settings.found_share:
            movement = stock.add(key, size, "SELLABLE", at, batch_id, references.get(key[1], Decimal(1)))
            reason = "FOUND"
        else:
            size = min(size, stock.free(key, day.day), book_qty)
            if size <= 0:
                continue
            movement = stock.take(key, size, "SELLABLE", at, batch_id)
            reason = "THEFT_OR_LOSS"
        per_warehouse[key[0]].append(AdjustmentLine(
            key[1], batch_id, "SELLABLE", movement.quantity, movement.unit_cost, movement.value_change, reason,
            location, system_quantity=book_qty, counted_quantity=book_qty + movement.quantity))
    for warehouse_id, lines in sorted(per_warehouse.items()):
        post_adjustment(day, sites[warehouse_id], "CYCLE_COUNT", "Monthly cycle count", max(
            counted_at[(warehouse_id, l.product_id)] for l in lines), lines, day.rng(f"inventory.count.{warehouse_id}"))
    stock.write(counted_at)


# ------------------------------------------------------------------------------------------ handling damage


def handling_damage(day: DayContext) -> None:
    if not day.is_working_day:
        return
    settings = day.world.config.inventory
    sites = warehouses(day)
    events: list[tuple[int, Any]] = []
    for warehouse_id, site in sorted(sites.items()):
        rng = day.rng(f"inventory.damage.{warehouse_id}")
        for _ in range(poisson(rng, settings.handling_damage_per_month[site["warehouse_type"]] / 26)):
            events.append((warehouse_id, rng))
    if not events:
        return
    expired = expired_stock(day)
    candidates: dict[int, list[int]] = {}
    for warehouse_id in sorted({w for w, _ in events}):
        candidates[warehouse_id] = [r["product_id"] for r in day.rows("""
            SELECT product_id, qty_sellable - qty_reserved AS free FROM inventory
            WHERE warehouse_id = :w AND qty_sellable - qty_reserved >= 3 ORDER BY product_id""", w=warehouse_id)
            if (warehouse_id, r["product_id"]) not in expired or r["free"] - expired[(warehouse_id, r["product_id"])] >= 3]
    picks = [(w, rng.choice(candidates[w]), rng) for w, rng in events if candidates[w]]
    stock = StockBook(day, {(w, p) for w, p, _ in picks})
    for warehouse_id, product_id, rng in picks:
        key = (warehouse_id, product_id)
        batch_id = stock.largest_batch(key, "SELLABLE", day.day) if stock.tracked(key) else None
        if stock.tracked(key) and batch_id is None:
            continue
        qty = min(Decimal(rng.randint(1, 3)), stock.free(key, day.day), stock.batch_qty(key, "SELLABLE", batch_id))
        if qty <= 0:
            continue
        moved = stock.last_moved(key)
        at = max(day.at(18, rng.randint(0, 59), rng.randint(0, 59)), (moved or day.at(0)) + timedelta(minutes=5))
        if at.date() != day.day:
            continue
        mac = stock.move(key, qty, "SELLABLE", "DEFECTIVE", at, batch_id)
        site = sites[warehouse_id]
        post_adjustment(day, site, "DAMAGE", "Damaged in handling", at, [
            AdjustmentLine(product_id, batch_id, "SELLABLE", -qty, mac, ZERO, "DAMAGED",
                           shelf_location(day, site, product_id)),
            AdjustmentLine(product_id, batch_id, "DEFECTIVE", qty, mac, ZERO, "DAMAGED",
                           defective_location(day, warehouse_id), defect_reason="HANDLING_DAMAGE"),
        ], rng)
    stock.write()


# ------------------------------------------------------------------------------------------ dispositions


def dispositions(day: DayContext) -> None:
    """Weekly: the warehouse manager disposes of defective stock (P18 for scrap and return to supplier)."""
    settings = day.world.config.inventory
    if not day.is_working_day or day.day.weekday() != settings.disposition_weekday:
        return
    pending = day.rows("""
        SELECT d.defective_stock_id, d.defect_number, d.warehouse_id, d.product_id, d.inventory_batch_id, d.quantity,
               d.detected_at, b.industry_pack_code, s.supplier_id AS preferred_supplier
        FROM defective_stock d JOIN products p ON p.product_id = d.product_id
        JOIN business_units b ON b.business_unit_id = p.business_unit_id
        LEFT JOIN supplier_products s ON s.preferred_key = d.product_id
        WHERE d.disposition_status = 'PENDING' AND d.detected_at < :cut ORDER BY d.defective_stock_id""",
        cut=day.at(0) - timedelta(days=settings.disposition_min_days - 1))
    if not pending:
        return
    sites = warehouses(day)
    packs = {p.code: p for p in day.world.packs}
    stock = StockBook(day, {(d["warehouse_id"], d["product_id"]) for d in pending})
    managers = {w: _crew(day, w) for w in sorted({d["warehouse_id"] for d in pending})}
    book = account_book(day)
    for defect in pending:
        key = (defect["warehouse_id"], defect["product_id"])
        rng = day.streams.stream(defect["detected_at"].date(), f"inventory.disposition.{defect['defective_stock_id']}")
        qty = defect["quantity"]
        if key not in stock.rows or stock.batch_qty(key, "DEFECTIVE", defect["inventory_batch_id"]) < qty:
            continue
        mix = packs[defect["industry_pack_code"]].dispositions
        roll, choice = rng.random(), "SCRAP"
        for option in ("SCRAP", "RETURN_TO_SUPPLIER", "REPAIR"):
            roll -= float(mix[option])
            if roll < 0:
                choice = option
                break
        if choice == "RETURN_TO_SUPPLIER" and defect["preferred_supplier"] is None:
            choice = "SCRAP"
        moved = stock.last_moved(key)
        at = max(day.at(18, rng.randint(0, 59), rng.randint(0, 59)), (moved or day.at(0)) + timedelta(minutes=5),
                 defect["detected_at"] + timedelta(minutes=5))
        if at.date() != day.day:
            continue
        site = sites[defect["warehouse_id"]]
        rows, repair_cost = [], None
        if choice == "REPAIR":
            mac = stock.move(key, qty, "DEFECTIVE", "SELLABLE", at, defect["inventory_batch_id"])
            repair_cost = money(product(qty, mac, Decimal(str(rng.uniform(*settings.repair_cost_share)))))
            rows = [("DEFECTIVE", -qty, mac, ZERO, defective_location(day, defect["warehouse_id"])),
                    ("SELLABLE", qty, mac, ZERO, shelf_location(day, site, defect["product_id"]))]
        else:
            movement = stock.take(key, qty, "DEFECTIVE", at, defect["inventory_batch_id"])
            rows = [("DEFECTIVE", -qty, movement.unit_cost, movement.value_change,
                     defective_location(day, defect["warehouse_id"]))]
        for status, signed, cost, value, location in rows:
            day.writer.insert("inventory_transactions", {
                "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
                "warehouse_id": defect["warehouse_id"], "product_id": defect["product_id"],
                "inventory_batch_id": defect["inventory_batch_id"], "warehouse_location_id": location,
                "transaction_type": "DEFECTIVE_DISPOSITION", "stock_status": status, "quantity": signed,
                "unit_cost": cost, "value_change": value, "goods_receipt_line_item_id": None,
                "shipment_line_item_id": None, "invoice_line_item_id": None, "sales_return_line_item_id": None,
                "return_inspection_id": None, "stock_transfer_line_item_id": None,
                "stock_adjustment_line_item_id": None, "defective_stock_id": defect["defective_stock_id"],
                "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
                "created_at": at, "updated_at": at,
            })
        day.writer.update("defective_stock", "defective_stock_id", defect["defective_stock_id"], {
            "disposition": choice, "disposition_status": "COMPLETED", "disposed_at": at,
            "disposed_by_employee_id": rng.choice(managers[defect["warehouse_id"]]),
            "supplier_id": defect["preferred_supplier"] if choice == "RETURN_TO_SUPPLIER" else None,
            "repair_cost_amount": repair_cost, "updated_at": at})
        if choice != "REPAIR" and rows[0][3]:
            value = -rows[0][3]
            journal = (JournalBuilder(f"Defective stock {defect['defect_number']}: {choice.lower().replace('_', ' ')}")
                       .debit(book.role("INVENTORY_WRITE_OFF"), value, cost_center_id=site["cost_center_id"])
                       .credit(book.role("INVENTORY"), value, warehouse_id=defect["warehouse_id"]).build())
            post_journal(day, journal, source="DEFECTIVE_DISPOSITION", defective_stock_id=defect["defective_stock_id"])
    stock.write()
