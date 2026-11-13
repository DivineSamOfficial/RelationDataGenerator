"""Inventory housekeeping (1G §2.2 step 9; 1C §5; step 3.12).

``morning_stock_step`` runs early in the day, before procurement, while nothing else has moved: the expiry sweep
(06:00–06:29), the monthly cycle counts (06:30–06:59) and the receipt of transfers that travelled overnight
(07:00–08:59). Every later movement of the day comes after them in time and in execution (V-IN-09 replays the
moving-average cost in time order). ``housekeeping_step`` runs after returns: handling damage and defective
dispositions, then the day's store replenishment and DC rebalancing transfers leave in the evening, each after
the last movement of its product in that warehouse.
"""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.inventory.adjustments import cycle_counts, dispositions, expiry_sweep, handling_damage
from scmgen.generation.engine.inventory.transfers import dispatch_step, transfer_receipts_step


def morning_stock_step(day: DayContext) -> None:
    expiry_sweep(day)
    cycle_counts(day)
    transfer_receipts_step(day)


def housekeeping_step(day: DayContext) -> None:
    handling_damage(day)
    dispositions(day)
    dispatch_step(day)


__all__ = ["housekeeping_step", "morning_stock_step"]
