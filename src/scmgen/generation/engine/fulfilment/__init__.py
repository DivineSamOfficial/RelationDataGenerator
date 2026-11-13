"""Fulfilment day step (1G §2.2 step 6; 1D §2, §4-5; steps 3.9a-b).

1. The carriers' events for parcels dispatched on earlier days (deliveries, COD collection, returns to origin,
   cancellations before transit) and the customer returns arriving today: first, because returned or reinstated
   stock must be in place, at its time, before today's issues (moving-average cost, V-IN-09).
2. Store handovers: counter sales at once, store pickups when the customer comes.
3. The distribution centres' dispatch wave with its invoices.
4. The first scans (and same-day hyperlocal deliveries) of the parcels dispatched today.
"""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.carrier import carrier_step
from scmgen.generation.engine.fulfilment.dispatch import dispatch_step
from scmgen.generation.engine.fulfilment.handover import handover_step
from scmgen.generation.engine.fulfilment.invoicing import Invoicer
from scmgen.generation.engine.fulfilment.outcomes import apply_outcomes


def fulfilment_step(day: DayContext) -> None:
    invoicer = Invoicer(day)
    apply_outcomes(day, invoicer, carrier_step(day, invoicer, dispatched_today=False), with_returns=True)
    handover_step(day, invoicer)
    dispatch_step(day, invoicer)
    apply_outcomes(day, invoicer, carrier_step(day, invoicer, dispatched_today=True))
