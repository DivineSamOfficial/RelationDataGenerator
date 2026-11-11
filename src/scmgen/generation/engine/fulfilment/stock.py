"""Stock issue for dispatches and store handovers (1C §4-5; D1, D27, D33; step 3.9a).

A sale takes stock out of SELLABLE at the warehouse's moving-average cost (``StockPosition.issue``); the
issued value is the sale's COGS (D52). Batch-tracked products are picked **first expiry, first out** (FEFO;
batches without an expiry date by receipt date), one ledger row per batch, and an expired batch is never
picked (V-IN-04). Batch balances are not stored anywhere: they are the ledger's sums per batch (1C §5), read
once for the products issued today. Issued stock always comes out of a reservation, so ``qty_reserved``
falls with ``qty_sellable``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from scmgen.domain.costing import Movement, StockPosition
from scmgen.generation.engine.day import DayContext


@dataclass
class _Batch:
    batch_id: int
    quantity: Decimal
    expiry: date | None
    received: date


@dataclass(frozen=True)
class Piece:
    """One ledger row of an issue: the batch (if any), quantity and the movement amounts."""
    batch_id: int | None
    quantity: Decimal
    movement: Movement
    location_id: int


class Issuer:
    """Today's stock positions and batch balances of the warehouse × product pairs being issued."""

    def __init__(self, day: DayContext, pairs: set[tuple[int, int]]) -> None:
        self.day = day
        self.positions: dict[tuple[int, int], dict[str, Any]] = {}
        self.batches: dict[tuple[int, int], list[_Batch]] = {}
        self.locations: dict[int, list[int]] = {}
        self.issued: dict[tuple[int, int], dict[str, Any]] = {}
        if not pairs:
            return
        warehouses = sorted({w for w, _ in pairs})
        products = sorted({p for _, p in pairs})
        in_w, in_p = ", ".join(map(str, warehouses)), ", ".join(map(str, products))
        for row in day.rows(f"""
                SELECT i.inventory_id, i.warehouse_id, i.product_id, i.qty_sellable, i.qty_quarantine, i.qty_defective,
                       i.stock_value, i.last_unit_cost, i.last_receipt_at, p.is_batch_tracked
                FROM inventory i JOIN products p ON p.product_id = i.product_id
                WHERE i.warehouse_id IN ({in_w}) AND i.product_id IN ({in_p})"""):
            key = (row["warehouse_id"], row["product_id"])
            if key in pairs:
                on_hand = row["qty_sellable"] + row["qty_quarantine"] + row["qty_defective"]
                self.positions[key] = {**row, "position": StockPosition(on_hand, row["stock_value"], row["last_unit_cost"])}
        tracked = sorted({p for (w, p), row in self.positions.items() if row["is_batch_tracked"]})
        if tracked:
            for row in day.rows(f"""
                    SELECT t.warehouse_id, t.product_id, t.inventory_batch_id, SUM(t.quantity) AS quantity,
                           b.expiry_date, b.received_date
                    FROM inventory_transactions t JOIN inventory_batches b ON b.inventory_batch_id = t.inventory_batch_id
                    WHERE t.stock_status = 'SELLABLE' AND t.warehouse_id IN ({in_w})
                      AND t.product_id IN ({", ".join(map(str, tracked))})
                    GROUP BY t.warehouse_id, t.product_id, t.inventory_batch_id, b.expiry_date, b.received_date
                    HAVING SUM(t.quantity) > 0"""):
                key = (row["warehouse_id"], row["product_id"])
                if key in pairs:
                    self.batches.setdefault(key, []).append(
                        _Batch(row["inventory_batch_id"], row["quantity"], row["expiry_date"], row["received_date"]))
            for batches in self.batches.values():
                batches.sort(key=lambda b: (b.expiry or date.max, b.received, b.batch_id))
        for row in day.rows(f"""
                SELECT l.warehouse_location_id, l.warehouse_id FROM warehouse_locations l
                JOIN warehouses w ON w.warehouse_id = l.warehouse_id
                WHERE l.is_active AND l.warehouse_id IN ({in_w})
                  AND l.zone = IF(w.warehouse_type = 'RETAIL_STORE', 'SALES_FLOOR', 'STORAGE')
                ORDER BY l.warehouse_location_id"""):
            self.locations.setdefault(row["warehouse_id"], []).append(row["warehouse_location_id"])

    def _usable(self, key: tuple[int, int], on: date) -> list[_Batch] | None:
        """Batches that may be picked on ``on`` (not expired), FEFO order; None for untracked products."""
        if not self.positions[key]["is_batch_tracked"]:
            return None
        return [b for b in self.batches.get(key, []) if b.quantity > 0 and (b.expiry is None or b.expiry > on)]

    def arrived_at(self, warehouse_id: int, product_id: int) -> datetime | None:
        """When the latest receipt of the product reached the warehouse (stock can't leave before it arrived)."""
        return self.positions[(warehouse_id, product_id)]["last_receipt_at"]

    def pickable(self, warehouse_id: int, product_id: int, qty: Decimal, on: date) -> Decimal:
        """How much of ``qty`` can be picked today (less only when unexpired batches run short)."""
        usable = self._usable((warehouse_id, product_id), on)
        if usable is None:
            return qty
        return min(qty, sum((b.quantity for b in usable), Decimal(0)))

    def issue(self, warehouse_id: int, product_id: int, qty: Decimal, at: datetime) -> list[Piece]:
        """Issue ``qty`` (already checked with ``pickable``): one piece per batch, at the current MAC."""
        key = (warehouse_id, product_id)
        state = self.positions[key]
        locations = self.locations[warehouse_id]
        location = locations[product_id % len(locations)]
        usable = self._usable(key, at.date())
        parts: list[tuple[_Batch | None, Decimal]] = []
        if usable is None:
            parts.append((None, qty))
        else:
            left = qty
            for batch in usable:
                take = min(batch.quantity, left)
                parts.append((batch, take))
                left -= take
                if left == 0:
                    break
            if left > 0:
                raise RuntimeError(f"Not enough unexpired batch stock of product {product_id} at warehouse {warehouse_id}")
        pieces = []
        for batch, take in parts:
            state["position"], movement = state["position"].issue(take)
            if batch is not None:
                batch.quantity -= take
            pieces.append(Piece(batch.batch_id if batch else None, take, movement, location))
        done = self.issued.setdefault(key, {"quantity": Decimal(0), "at": at})
        done["quantity"] += qty
        done["at"] = max(done["at"], at)
        return pieces

    def write(self) -> None:
        """Sellable and reserved quantity fall by what was issued; value and MAC follow the position."""
        changes = []
        for key, done in sorted(self.issued.items()):
            state = self.positions[key]
            changes.append({"i": state["inventory_id"], "q": done["quantity"], "v": state["position"].value,
                            "t": done["at"]})
        if changes:
            self.day.execute("""
                UPDATE inventory SET qty_sellable = qty_sellable - :q, qty_reserved = qty_reserved - :q,
                       stock_value = :v, last_issue_at = :t, updated_at = GREATEST(updated_at, :t)
                WHERE inventory_id = :i""", changes)
        self.issued.clear()


def ledger_row(day: DayContext, warehouse_id: int, product_id: int, piece: Piece, at: datetime, *,
               shipment_line_item_id: int | None = None, invoice_line_item_id: int | None = None) -> dict[str, Any]:
    """The SALES_ISSUE ledger row of one piece (source: the shipment line, or the invoice line of a handover)."""
    return {
        "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
        "warehouse_id": warehouse_id, "product_id": product_id, "inventory_batch_id": piece.batch_id,
        "warehouse_location_id": piece.location_id, "transaction_type": "SALES_ISSUE", "stock_status": "SELLABLE",
        "quantity": piece.movement.quantity, "unit_cost": piece.movement.unit_cost,
        "value_change": piece.movement.value_change, "goods_receipt_line_item_id": None,
        "shipment_line_item_id": shipment_line_item_id, "invoice_line_item_id": invoice_line_item_id,
        "sales_return_line_item_id": None, "return_inspection_id": None, "stock_transfer_line_item_id": None,
        "stock_adjustment_line_item_id": None, "defective_stock_id": None,
        "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
        "created_at": at, "updated_at": at,
    }
