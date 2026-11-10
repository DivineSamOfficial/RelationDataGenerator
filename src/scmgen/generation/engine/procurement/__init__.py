"""Procurement day step (1G §2.2 step 4; 1C §3): sourcing (3.7a), receiving (3.7b), supplier
invoices and payments (3.7c)."""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.procurement.payables import invoice_approvals, payment_run, supplier_invoicing
from scmgen.generation.engine.procurement.receiving import receiving
from scmgen.generation.engine.procurement.sourcing import po_approvals, replenishment, rfq_evaluations, rfq_responses


def procurement_step(day: DayContext) -> None:
    """Purchasing and the receiving dock work on working days only. Order matters: answers before awards,
    approvals before deliveries (a PO is received only once sent), and everything before the stock check, so
    today's receipts and new POs count in the stock position."""
    if not day.is_working_day:
        return
    rfq_responses(day)
    rfq_evaluations(day)
    po_approvals(day)
    receiving(day)
    supplier_invoicing(day)
    invoice_approvals(day)
    payment_run(day)
    replenishment(day)
