"""Fulfilment day step (1G §2.2 step 6; 1D §2, §4-5; step 3.9a): store handovers (counter sales at once, store
pickups when the customer comes), then the distribution centres' dispatch wave with its invoices."""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.dispatch import dispatch_step
from scmgen.generation.engine.fulfilment.handover import handover_step
from scmgen.generation.engine.fulfilment.invoicing import Invoicer


def fulfilment_step(day: DayContext) -> None:
    invoicer = Invoicer(day)
    handover_step(day, invoicer)
    dispatch_step(day, invoicer)
