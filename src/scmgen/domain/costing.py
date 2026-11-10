"""Moving weighted average cost per warehouse × product (D1, D27, 1C §4).

One ``StockPosition`` mirrors an ``inventory`` row: on-hand quantity Q (all status buckets) and stock
value V. MAC = V / Q, computed exactly like the stored column
``moving_average_cost = ROUND(stock_value / qty_on_hand, 4)`` (MySQL's division rounding, D87), so the
engine's view of cost never drifts from the database's.

Rules:
* Receipts (purchase, return, transfer in, found stock, opening stock) add ``ROUND(q × cost, 2)``.
* Issues (sale, transfer out, write-off) take ``ROUND(q × MAC, 2)``; issuing the whole quantity takes
  exactly the remaining value, so no stray paise stay behind (V = 0 ⇔ Q = 0, a CHECK on ``inventory``).
* Status moves (sellable ↔ quarantine ↔ defective) change neither Q nor V here.
* Positions are immutable: every movement returns a new position plus the ledger amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from scmgen.core.money import ZERO, money, product, quantity, round_half_up, sql_divide, unit_price


class StockError(ValueError):
    """A movement that would break an inventory invariant (negative stock, bad quantity)."""


@dataclass(frozen=True)
class Movement:
    """What a movement writes to the ledger (``inventory_transactions``): signed quantity and value."""

    quantity: Decimal       # + receipt / − issue
    unit_cost: Decimal      # cost per unit snapshot, 4 decimals
    value_change: Decimal   # signed, 2 decimals


@dataclass(frozen=True)
class StockPosition:
    quantity: Decimal = ZERO
    value: Decimal = ZERO
    last_unit_cost: Decimal | None = None      # kept when Q = 0, for found-stock valuation (1C §4)

    def __post_init__(self) -> None:
        if self.quantity < 0 or self.value < 0:
            raise StockError(f"Negative stock position: Q={self.quantity}, V={self.value}")
        if (self.quantity == 0) != (self.value == 0):
            raise StockError(f"Value {self.value} with quantity {self.quantity}: V = 0 must coincide with Q = 0")

    @property
    def moving_average_cost(self) -> Decimal | None:
        """``ROUND(V / Q, 4)`` as MySQL stores it; None when there is no stock."""
        if self.quantity == 0:
            return None
        return round_half_up(sql_divide(self.value, self.quantity, 2), 4)

    def receive(self, qty: Decimal | int | str, cost: Decimal | int | str) -> tuple[StockPosition, Movement]:
        """Stock in at a given unit cost (PO net price, original issue cost, transfer cost …)."""
        q, c = quantity(qty), unit_price(cost)
        if q <= 0:
            raise StockError(f"Receipt quantity must be positive, got {q}")
        if c < 0:
            raise StockError(f"Unit cost can't be negative, got {c}")
        value = money(product(q, c))
        if value == 0:
            # A zero-value receipt would leave Q > 0 with V = 0 (forbidden): free goods carry a token cost.
            raise StockError(f"Receipt of {q} at {c} has no value; value it at a positive cost")
        position = StockPosition(self.quantity + q, self.value + value, c)
        return position, Movement(q, c, value)

    def issue(self, qty: Decimal | int | str) -> tuple[StockPosition, Movement]:
        """Stock out at the current MAC; the cost of a sale becomes COGS (D52)."""
        q = quantity(qty)
        if q <= 0:
            raise StockError(f"Issue quantity must be positive, got {q}")
        if q > self.quantity:
            raise StockError(f"Issue of {q} exceeds on-hand {self.quantity}: inventory never goes negative")
        mac = self.moving_average_cost
        assert mac is not None
        if q == self.quantity:
            value = self.value                                     # clear the position exactly
        else:
            value = min(money(product(q, mac)), self.value)
            if value == self.value:
                # Rounding would empty the value while units remain; keep a paisa with them.
                value = self.value - Decimal("0.01")
        position = StockPosition(self.quantity - q, self.value - value, mac)
        return position, Movement(-q, mac, -value)

    def receive_found(self, qty: Decimal | int | str, reference_cost: Decimal | int | str) -> tuple[StockPosition, Movement]:
        """Found stock (adjustment in, 1C §4): valued at MAC, or the reference cost when Q = 0."""
        cost = self.moving_average_cost
        if cost is None or cost == 0:
            cost = self.last_unit_cost if self.last_unit_cost else unit_price(reference_cost)
        return self.receive(qty, cost)
