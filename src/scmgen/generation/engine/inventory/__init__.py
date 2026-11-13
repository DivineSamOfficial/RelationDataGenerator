"""Inventory housekeeping (1G §2.2 step 9; 1C §5; step 3.12).

``transfer_receipts_step`` runs early in the day, before procurement: goods that travelled overnight are unloaded
before any other stock moves, so every later movement of the day comes after them in time and in execution
(V-IN-09 replays the moving-average cost in time order). ``housekeeping_step`` runs after returns: the day's store
replenishment and DC rebalancing transfers are dispatched in the evening.
"""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.inventory.transfers import dispatch_step, transfer_receipts_step


def housekeeping_step(day: DayContext) -> None:
    dispatch_step(day)


__all__ = ["housekeeping_step", "transfer_receipts_step"]
