"""Sales day step, demand side (1G §2.2 step 5; 1D §2-3): credit-hold reviews, then today's new orders with
their checkout (prepaid payments, COD, counter sales, B2B credit checks). Step 3.8a."""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.sales.checkout import Checkout, place, review_holds
from scmgen.generation.engine.sales.demand import draft_orders
from scmgen.generation.engine.sales.model import sales_model


def sales_step(day: DayContext) -> None:
    """Online orders arrive every day; store counters and Credit Control work on working days only."""
    model = sales_model(day)
    checkout = Checkout(day, model)
    review_holds(day, checkout)
    for draft in draft_orders(day, model):
        place(day, checkout, draft)
