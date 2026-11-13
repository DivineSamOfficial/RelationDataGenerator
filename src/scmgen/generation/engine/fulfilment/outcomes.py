"""What the carrier's events mean for orders, stock and the books (1D §2, §4; D40, D41; 1E P07, P11, P13;
step 3.9b). Runs after the day's feed events, in time order (stock events change the moving-average cost):

* **Delivered**: the shipment's lines are delivered (order-line counters; ``PARTIALLY_DELIVERED`` / ``DELIVERED``).
* **Returned to origin** (D41): the parcel is back at the warehouse: ``RETURNED_TO_SENDER``, an ``RTO_RECEIPT`` per
  shipment line (+SELLABLE, its batch, at the original issue cost), **P13** (Dr Inventory / Cr COGS per business
  unit), the order lines' RTO counters, and a full **RTO credit note** for the shipment's invoice (P11).
* **Cancelled before transit** (D40): the customer cancelled after dispatch, before the parcel left the origin hub
  and within 24 h of the invoice: the shipment and invoice are cancelled, the stock issue is reversed (reversal
  rows, D33), **P07** reverses P06 (D61), negative mirror tax rows, the invoice's AR item is reversed and its
  allocations undone (the prepayment is open again until it is refunded, 3.11), the order is cancelled.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from scmgen.domain.costing import StockPosition
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import post_journal, reverse_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver, record_status
from scmgen.generation.engine.fulfilment.carrier import CarrierDay
from scmgen.generation.engine.fulfilment.credit_notes import issue_credit_note, load_invoices
from scmgen.generation.engine.fulfilment.invoicing import Invoicer, ar_state
from scmgen.generation.engine.fulfilment.orders import OrderBook
from scmgen.generation.engine.order_status import line_status, open_quantity, order_status

CANCEL_TEXT = {"CUSTOMER_REQUEST": "Cancelled at the customer's request before transit",
               "DUPLICATE_ORDER": "Duplicate order cancelled before transit"}


def apply_outcomes(day: DayContext, invoicer: Invoicer, events: CarrierDay) -> None:
    shipments = [s for s, _ in events.delivered] + [s for s, _ in events.returned] + [s for s, _, _ in events.cancelled]
    if not shipments:
        return
    order_ids = sorted({s["order_id"] for s in shipments})
    book = OrderBook.load(day, f"o.order_id IN ({', '.join(map(str, order_ids))})")
    lines = _shipment_lines(day, [s["shipment_id"] for s in shipments])
    for shipment, at in events.delivered:
        for line in lines[shipment["shipment_id"]]:
            book.lines[line["order_line_item_id"]]["quantity_delivered"] += line["quantity_shipped"]
            book.touch_line(line["order_line_item_id"], at)
    book.write_orders("CARRIER", "Delivered")

    stock_events = [(at, "RTO", s, None) for s, at in events.returned] + \
                   [(at, "CANCEL", s, reason) for s, at, reason in events.cancelled]
    if not stock_events:
        return
    invoices = load_invoices(day, [s["invoice_id"] for _, _, s, _ in stock_events])
    stock = _Positions(day, {(s["warehouse_id"], l["product_id"]) for _, _, s, _ in stock_events
                             for l in lines[s["shipment_id"]]})
    approvals = {s["shipment_id"]: approver(day, invoices[s["invoice_id"]]["total_amount"], ("FIN",), day.day)
                 for _, kind, s, _ in stock_events if kind == "RTO"}
    reservations = _reservations(day, [s for _, kind, s, _ in stock_events if kind == "CANCEL"])
    for at, kind, shipment, reason in sorted(stock_events, key=lambda e: (e[0], e[2]["shipment_id"])):
        if kind == "RTO":
            _return_to_origin(day, invoicer, book, stock, shipment, lines[shipment["shipment_id"]],
                              invoices[shipment["invoice_id"]], at, approvals[shipment["shipment_id"]])
        else:
            _cancel_before_transit(day, invoicer, stock, shipment, lines[shipment["shipment_id"]],
                                   invoices[shipment["invoice_id"]], reservations, book, at, reason)
    stock.write()
    book.write_orders("CARRIER", "Returned to origin")


def _shipment_lines(day: DayContext, shipment_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Shipment lines with their original issue (cost, batch, location) and product business unit."""
    found: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in day.rows(f"""
            SELECT sl.shipment_line_item_id, sl.shipment_id, sl.order_line_item_id, sl.quantity_shipped,
                   sl.inventory_batch_id, l.product_id, p.business_unit_id, b.expiry_date,
                   t.inventory_transaction_id AS issue_id, t.unit_cost, t.value_change, t.warehouse_location_id
            FROM shipment_line_items sl JOIN order_line_items l ON l.order_line_item_id = sl.order_line_item_id
            JOIN products p ON p.product_id = l.product_id
            LEFT JOIN inventory_batches b ON b.inventory_batch_id = sl.inventory_batch_id
            JOIN inventory_transactions t ON t.shipment_line_item_id = sl.shipment_line_item_id
                 AND t.transaction_type = 'SALES_ISSUE' AND t.reversal_of_inventory_transaction_id IS NULL
            WHERE sl.shipment_id IN ({", ".join(map(str, sorted(set(shipment_ids))))})
            ORDER BY sl.shipment_id, sl.line_number"""):
        found[row["shipment_id"]].append(row)
    return found


def _reservations(day: DayContext, shipments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not shipments:
        return []
    return day.rows(f"""
        SELECT r.inventory_reservation_id, r.order_line_item_id, r.warehouse_id, r.fulfilled_quantity,
               r.released_quantity, r.outstanding_quantity, r.updated_at, l.order_id
        FROM inventory_reservations r JOIN order_line_items l ON l.order_line_item_id = r.order_line_item_id
        WHERE l.order_id IN ({", ".join(str(s["order_id"]) for s in shipments)})
        ORDER BY r.inventory_reservation_id""")


class _Positions:
    """Stock positions of the warehouse × product pairs that get goods back today."""

    def __init__(self, day: DayContext, pairs: set[tuple[int, int]]) -> None:
        self.day = day
        self.positions: dict[tuple[int, int], dict[str, Any]] = {}
        self.changed: dict[tuple[int, int], dict[str, Any]] = {}
        if pairs:
            for row in day.rows(f"""
                    SELECT inventory_id, warehouse_id, product_id, qty_sellable + qty_quarantine + qty_defective AS on_hand,
                           stock_value, last_unit_cost FROM inventory
                    WHERE (warehouse_id, product_id) IN ({", ".join(f"({w}, {p})" for w, p in sorted(pairs))})"""):
                self.positions[(row["warehouse_id"], row["product_id"])] = {
                    **row, "position": StockPosition(row["on_hand"], row["stock_value"], row["last_unit_cost"])}

    def back_in(self, warehouse_id: int, product_id: int, qty: Decimal, value: Decimal, at: datetime,
                receipt: bool) -> None:
        state = self.positions[(warehouse_id, product_id)]
        old: StockPosition = state["position"]
        state["position"] = StockPosition(old.quantity + qty, old.value + value, old.last_unit_cost)
        change = self.changed.setdefault((warehouse_id, product_id), {"q": Decimal(0), "at": at, "receipt": None})
        change["q"] += qty
        change["at"] = max(change["at"], at)
        if receipt or change["receipt"]:
            change["receipt"] = at       # stock that arrived: today's later dispatches start after it

    def write(self) -> None:
        rows = []
        for key, change in sorted(self.changed.items()):
            state = self.positions[key]
            rows.append({"i": state["inventory_id"], "q": change["q"], "v": state["position"].value,
                         "t": change["at"], "r": change["at"]})
        if rows:
            self.day.execute("""
                UPDATE inventory SET qty_sellable = qty_sellable + :q, stock_value = :v,
                       last_receipt_at = GREATEST(COALESCE(last_receipt_at, :r), :r),
                       updated_at = GREATEST(updated_at, :t) WHERE inventory_id = :i""", rows)


def _ledger(day: DayContext, shipment: dict[str, Any], line: dict[str, Any], kind: str, at: datetime,
            value: Decimal, reversal: int | None) -> dict[str, Any]:
    return {
        "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": at,
        "warehouse_id": shipment["warehouse_id"], "product_id": line["product_id"],
        "inventory_batch_id": line["inventory_batch_id"], "warehouse_location_id": line["warehouse_location_id"],
        "transaction_type": kind, "stock_status": "SELLABLE", "quantity": line["quantity_shipped"],
        "unit_cost": line["unit_cost"], "value_change": value, "goods_receipt_line_item_id": None,
        "shipment_line_item_id": line["shipment_line_item_id"], "invoice_line_item_id": None,
        "sales_return_line_item_id": None, "return_inspection_id": None, "stock_transfer_line_item_id": None,
        "stock_adjustment_line_item_id": None, "defective_stock_id": None,
        "reversal_of_inventory_transaction_id": reversal, "generation_run_id": day.run_id,
        "created_at": at, "updated_at": at,
    }


def _shipment_status(day: DayContext, shipment: dict[str, Any], to_status: str, at: datetime, reason: str,
                     **values: Any) -> None:
    day.writer.update("shipments", "shipment_id", shipment["shipment_id"],
                      {"shipment_status": to_status, **values, "updated_at": at})
    record_status(day, "shipment", shipment["shipment_id"], shipment["status_now"], to_status, at, None, reason)


def _return_to_origin(day: DayContext, invoicer: Invoicer, book: OrderBook, stock: _Positions, shipment: dict[str, Any],
                      lines: list[dict[str, Any]], invoice: dict[str, Any], at: datetime, approved_by: int) -> None:
    """D41: goods back in stock at their issue cost (P13) and the invoice credited in full (P11)."""
    shipment["status_now"] = "DELIVERY_FAILED"
    _shipment_status(day, shipment, "RETURNED_TO_SENDER", at, "Returned to origin", rto_received_at=at)
    cogs: dict[int, Decimal] = defaultdict(Decimal)
    expired = day.world.refs.get("expired_stock")
    for line in lines:
        value = -line["value_change"]                      # back at the original issue cost
        day.writer.insert("inventory_transactions", _ledger(day, shipment, line, "RTO_RECEIPT", at, value, None))
        stock.back_in(shipment["warehouse_id"], line["product_id"], line["quantity_shipped"], value, at, receipt=True)
        cogs[line["business_unit_id"]] += value
        order_line = book.lines[line["order_line_item_id"]]
        order_line["quantity_rto"] += line["quantity_shipped"]
        book.touch_line(line["order_line_item_id"], at)
        if expired is not None and line["expiry_date"] is not None and line["expiry_date"] <= day.day:
            key = (shipment["warehouse_id"], line["product_id"])     # an expired batch came back (expiry view, D66)
            expired["quantity"][key] = expired["quantity"].get(key, Decimal(0)) + line["quantity_shipped"]
    book_ = invoicer.book
    builder = JournalBuilder(f"Return to origin {shipment['tracking_number']}")
    builder.debit(book_.role("INVENTORY"), sum(cogs.values(), Decimal(0)), warehouse_id=shipment["warehouse_id"])
    for unit in sorted(cogs):
        builder.credit(book_.cogs(unit), cogs[unit], cost_center_id=invoicer.bu_cc[unit])
    post_journal(day, builder.build(), source="RTO_RECEIPT", shipment_id=shipment["shipment_id"])
    issue_credit_note(day, invoicer, invoice, "RTO", at, approved_by)


def _cancel_before_transit(day: DayContext, invoicer: Invoicer, stock: _Positions, shipment: dict[str, Any],
                           lines: list[dict[str, Any]], invoice: dict[str, Any], reservations: list[dict[str, Any]],
                           book: OrderBook, at: datetime, reason: str) -> None:
    """D40: the invoice and the dispatch are undone; the order is cancelled."""
    text = CANCEL_TEXT[reason]
    reverse_journal(day, invoice["sales_journal_id"], f"Cancellation of invoice {invoice['invoice_number']}", day.day)
    shipment["status_now"] = "PICKED_UP"
    _shipment_status(day, shipment, "CANCELLED", at, text)
    day.writer.update("invoices", "invoice_id", invoice["invoice_id"], {
        "invoice_status": "CANCELLED", "cancelled_at": at, "cancellation_reason": text, "updated_at": at})
    for line in invoice["lines"]:
        product = line["line_type"] == "PRODUCT"
        day.writer.insert("tax_transactions", {
            "tax_transaction_id": day.ids.next("tax_transactions"),
            "company_gst_registration_id": invoice["company_gst_registration_id"], "tax_direction": "OUTPUT",
            "document_type": "SALES_INVOICE", "is_cancellation": True,
            "invoice_line_item_id": line["invoice_line_item_id"], "credit_note_line_item_id": None,
            "supplier_invoice_line_item_id": None, "expense_line_item_id": None, "stock_transfer_line_item_id": None,
            "document_number": invoice["invoice_number"], "document_date": day.day,
            "counterparty_gstin": invoice["customer_gstin"], "customer_id": invoice["customer_id"], "supplier_id": None,
            "place_of_supply_state_id": invoice["place_of_supply_state_id"], "is_inter_state": invoice["is_inter_state"],
            "hsn_code": line["hsn_code"], "uqc": line["gst_uqc"] if product else None,
            "quantity": -line["quantity"] if product else None, "taxable_value": -line["taxable_amount"],
            "gst_rate": line["gst_rate"], "cgst_amount": -line["cgst_amount"], "sgst_amount": -line["sgst_amount"],
            "igst_amount": -line["igst_amount"], "itc_eligibility": "NOT_APPLICABLE",
            "gstr1_section": _section(invoicer, invoice), "gst_return_id": None, "created_at": at, "updated_at": at,
        })
    _undo_receivable(day, invoice, at)
    issued: dict[int, Decimal] = defaultdict(Decimal)
    for line in lines:
        value = -line["value_change"]
        day.writer.insert("inventory_transactions",
                          _ledger(day, shipment, line, "SALES_ISSUE", at, value, line["issue_id"]))
        stock.back_in(shipment["warehouse_id"], line["product_id"], line["quantity_shipped"], value, at, receipt=True)
        issued[line["order_line_item_id"]] += line["quantity_shipped"]
    for reservation in reservations:
        if reservation["order_id"] != shipment["order_id"]:
            continue
        back = issued.get(reservation["order_line_item_id"], Decimal(0)) \
            if reservation["warehouse_id"] == shipment["warehouse_id"] else Decimal(0)
        released = reservation["outstanding_quantity"] + back
        if released <= 0:
            continue
        day.writer.update("inventory_reservations", "inventory_reservation_id", reservation["inventory_reservation_id"], {
            "fulfilled_quantity": reservation["fulfilled_quantity"] - back,
            "released_quantity": reservation["released_quantity"] + released, "reservation_status": "RELEASED",
            "closed_at": at, "release_reason": text, "updated_at": at})
    for line in [l for l in book.lines.values() if l["order_id"] == shipment["order_id"]]:
        back = issued.get(line["order_line_item_id"], Decimal(0))
        line["quantity_shipped"] -= back
        line["quantity_invoiced"] -= back
        line["quantity_cancelled"] = line["quantity_ordered"] - line["quantity_shipped"]
        book.touch_line(line["order_line_item_id"], at)
    order = book.orders[shipment["order_id"]]
    day.writer.update("orders", "order_id", order["order_id"], {
        "order_status": "CANCELLED", "cancelled_at": at, "cancellation_reason": reason, "updated_at": at})
    record_status(day, "order", order["order_id"], order["order_status"], "CANCELLED", at, None, text)
    order["order_status"] = "CANCELLED"


def _section(invoicer: Invoicer, invoice: dict[str, Any]) -> str:
    if invoice["customer_gstin"]:
        return "B2B"
    return "B2CL" if invoice["is_inter_state"] and invoice["total_amount"] > invoicer.b2cl_threshold else "B2CS"


def _undo_receivable(day: DayContext, invoice: dict[str, Any], at: datetime) -> None:
    """The invoice's AR item is reversed; its allocations are undone and the payments are open again (D49)."""
    for allocation in day.rows("""
            SELECT a.customer_payment_allocation_id, a.allocated_amount, a.customer_payment_id, r.accounts_receivable_id,
                   r.original_amount, r.open_amount
            FROM customer_payment_allocations a JOIN accounts_receivable r ON r.customer_payment_id = a.customer_payment_id
            WHERE a.invoice_id = :i AND a.allocation_status = 'ACTIVE'""", i=invoice["invoice_id"]):
        day.writer.update("customer_payment_allocations", "customer_payment_allocation_id",
                          allocation["customer_payment_allocation_id"],
                          {"allocation_status": "REVERSED", "reversed_at": at, "updated_at": at})
        day.writer.update("accounts_receivable", "accounts_receivable_id", allocation["accounts_receivable_id"],
                          ar_state(allocation["original_amount"],
                                   allocation["open_amount"] - allocation["allocated_amount"], at))
    day.writer.update("accounts_receivable", "accounts_receivable_id", invoice["accounts_receivable_id"], {
        "open_amount": Decimal(0), "item_status": "REVERSED", "settled_at": None, "updated_at": at})


__all__ = ["apply_outcomes", "line_status", "open_quantity", "order_status"]
