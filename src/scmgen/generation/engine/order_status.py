"""Order and order-line status from the line quantities (1D §3; steps 3.9a-b).

One rule set for every process that moves quantities (dispatch, store handover, delivery, return to origin,
backorder expiry, invoice cancellation), so an order's status always says the same thing about its lines:

* a line: ``CANCELLED`` when nothing is left open; ``DELIVERED`` when all of it was shipped and every shipped unit
  was delivered or came back to origin with some delivered, ``CLOSED`` when all of it came back (RTO, D41);
  ``SHIPPED`` / ``PARTIALLY_SHIPPED`` by shipped quantity; otherwise unchanged (``OPEN`` / ``BACKORDERED``);
* an order: ``CANCELLED`` when no line is open; ``DELIVERED`` / ``CLOSED`` when every open line is done (some /
  nothing delivered); ``PARTIALLY_DELIVERED`` when something was delivered but not everything is done;
  ``SHIPPED`` / ``PARTIALLY_SHIPPED`` by shipped lines; otherwise unchanged (still before dispatch).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable


def open_quantity(line: dict[str, Any]) -> Decimal:
    return line["quantity_ordered"] - line["quantity_cancelled"]


def _done(line: dict[str, Any]) -> bool:
    return (line["quantity_shipped"] == open_quantity(line)
            and line["quantity_delivered"] + line["quantity_rto"] == line["quantity_shipped"])


def line_status(line: dict[str, Any]) -> str:
    if open_quantity(line) == 0:
        return "CANCELLED"
    if _done(line):
        return "DELIVERED" if line["quantity_delivered"] > 0 else "CLOSED"
    if line["quantity_shipped"] > 0:
        return "SHIPPED" if line["quantity_shipped"] == open_quantity(line) else "PARTIALLY_SHIPPED"
    return line["line_status"]


def order_status(lines: Iterable[dict[str, Any]], current: str) -> str:
    live = [line for line in lines if open_quantity(line) > 0]
    if not live:
        return "CANCELLED"
    delivered = any(line["quantity_delivered"] > 0 for line in live)
    if all(_done(line) for line in live):
        return "DELIVERED" if delivered else "CLOSED"
    if delivered:
        return "PARTIALLY_DELIVERED"
    if any(line["quantity_shipped"] > 0 for line in live):
        return "SHIPPED" if all(line["quantity_shipped"] == open_quantity(line) for line in live) \
            else "PARTIALLY_SHIPPED"
    return current
