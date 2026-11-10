"""Sales day step, demand side (1G §2.2 step 5; 1C §5; 1D §2-3): credit-hold reviews, backorder release, today's
new orders with their checkout and stock reservations, then the customer cancellations that fall due.
Steps 3.8a (orders, checkout) and 3.8b (reservations, backorders, cancellations)."""

from __future__ import annotations

from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.sales.allocation import Stock, customer_cancellations, open_lines, release_backorders
from scmgen.generation.engine.sales.checkout import Checkout, place, review_holds
from scmgen.generation.engine.sales.demand import draft_orders
from scmgen.generation.engine.sales.model import sales_model


def sales_step(day: DayContext) -> None:
    """Online orders arrive every day; store counters and Credit Control work on working days only."""
    model = sales_model(day)
    checkout = Checkout(day, model)
    held = open_lines(day, ("ON_HOLD",))
    backordered = open_lines(day, ("BACKORDERED",), line_status="BACKORDERED")
    drafts = draft_orders(day, model)
    stock = Stock(day)
    stock.load({line["product_id"] for orders in (held, backordered) for order in orders.values()
                for line in order["lines"]} | {line["product_id"] for draft in drafts for line in draft.lines})
    checkout.preload_ar({order["customer_id"] for order in held.values()}
                        | {d.customer.customer_id for d in drafts if d.customer.kind == "B2B"})
    review_holds(day, checkout, stock, held)
    release_backorders(day, stock, backordered)
    for draft in drafts:
        place(day, checkout, stock, draft)
    customer_cancellations(day, stock)
    stock.write()
