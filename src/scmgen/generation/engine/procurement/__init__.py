"""Procurement day step (1G §2.2 step 4; 1C §3): sourcing (3.7a); receiving and invoicing follow in 3.7b/3.7c."""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.procurement.sourcing import po_approvals, replenishment, rfq_evaluations, rfq_responses


def procurement_step(day: DayContext) -> None:
    """Purchasing works on working days only. Order matters: answers before awards, awards (and their POs)
    before the stock check, so today's new POs count as stock on order."""
    if not day.is_working_day:
        return
    rfq_responses(day)
    rfq_evaluations(day)
    po_approvals(day)
    replenishment(day)
