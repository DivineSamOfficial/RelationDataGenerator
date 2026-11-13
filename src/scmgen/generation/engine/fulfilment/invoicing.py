"""Tax invoices at dispatch or handover (1D §5; D4, D38, D42, D46, D48, D49, D52, D79; 1E P06; step 3.9a).

One call of ``Invoicer.bill`` is one business event, written together:

* the **invoice** (D38 number from the issuing warehouse's GSTIN series; place of supply = ship-to state, or the
  store's state for a handover; CGST + SGST when that is the GSTIN's own state, IGST otherwise; e-invoice IRN for
  GST-registered buyers, D46) and its **lines**: one per shipment line (or per order line for a handover), the
  order line's price and discount at the **invoice-date** GST rate (a GST-inclusive B2C price stays the same and
  its taxable value is back-calculated, D79), plus the order's shipping charge on its first invoice, taxed at
  the invoice's highest goods rate (D42);
* one OUTPUT **tax transaction** per line, with its GSTR-1 section (B2B / B2CL / B2CS);
* the **P06 journal**: Dr AR (customer, total), Dr COGS per business unit (the issue values, D52), Cr Sales per
  business unit, Cr Shipping Income, Cr output GST of the issuing GSTIN, Cr Inventory (warehouse), round-off;
* the AR **open item** (D49) and, for a prepaid order, the **allocation** of its prepayment to the invoice.

Round-off: normally to the nearest rupee. When an invoice completes a prepaid order and the remaining prepayment
is within −0.99…+0.50 of the exact total, the round-off makes the total equal to it, so the customer's payment
clears the invoice exactly (the order estimate is rounded the same way, step 3.9a). The −0.99 covers a split order:
its first invoice rounded up and the estimate rounded down leave the last invoice up to ₹0.99 above the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO, money, total
from scmgen.domain.gst import GstError, document_totals, price_exclusive, price_inclusive, split_gst, tax_line
from scmgen.domain.identifiers import irn
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal, posting_period
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import preload_rates, rate_on
from scmgen.core.calendar import fiscal_year, fiscal_year_label
from scmgen.generation.numbering import Registration

SHIPPING_SAC = "996812"            # courier / delivery services (D42)
MAX_ROUND_OFF = Decimal("0.50")
MIN_COMPLETING_ROUND_OFF = Decimal("-0.99")   # an invoice that completes a prepaid order


@dataclass
class BillLine:
    """A quantity of an order line on the invoice, with its stock issue value (COGS)."""
    order_line: dict[str, Any]          # order_line_item_id, product_id, unit_price, inclusive_unit_price, discount_percent
    quantity: Decimal
    cost: Decimal                       # issue value (positive)
    shipment_line_item_id: int | None = None
    invoice_line_item_id: int = 0       # set by ``bill``


@dataclass
class Bill:
    """What ``bill`` wrote: ids and amounts the caller needs (COD amount, payment amount, ledger sources)."""
    invoice_id: int
    invoice_number: str
    total_amount: Decimal
    lines: list[BillLine] = field(default_factory=list)


class Invoicer:
    """Per-day resources of invoicing: GSTINs, warehouses, cost centres, prepayments and product data."""

    def __init__(self, day: DayContext) -> None:
        self.day = day
        self.book = account_book(day)
        posting_period(day, day.day)        # warm the per-day cache now: a query later would flush half a document
        self.registrations = {r["company_gst_registration_id"]: r for r in day.rows("""
            SELECT g.company_gst_registration_id, g.gstin, g.state_id, s.state_code FROM company_gst_registrations g
            JOIN states s ON s.state_id = g.state_id""")}
        self.warehouses = {r["warehouse_id"]: r for r in day.rows(
            "SELECT warehouse_id, warehouse_type, company_gst_registration_id, state_id, city FROM warehouses")}
        self.bu_cc = {r["business_unit_id"]: r["cost_center_id"] for r in day.rows(
            "SELECT business_unit_id, cost_center_id FROM cost_centers WHERE business_unit_id IS NOT NULL")}
        self.finance_cc = day.rows("SELECT c.cost_center_id FROM cost_centers c JOIN departments d USING (department_id) "
                                   "WHERE d.department_code = 'FIN'")[0]["cost_center_id"]
        self.terms = {r["payment_term_id"]: r for r in day.rows(
            "SELECT payment_term_id, term_code, due_days, is_prepaid FROM payment_terms")}
        self.b2cl_threshold = Decimal(day.world.config.fulfilment.b2cl_threshold)
        self.products: dict[int, dict[str, Any]] = {}
        self.customers: dict[int, dict[str, Any]] = {}
        self.prepayments: dict[int, dict[str, Any]] = {}      # order → its SUCCESS prepayment's AR item
        self.shipping_billed: set[int] = set()                 # orders whose shipping charge is on an issued invoice
        self.invoice_items: dict[int, dict[str, Any]] = {}     # invoice → its AR item written today
        self.fy_label = fiscal_year_label(fiscal_year(day.day, day.world.config.fiscal_year_start_month),
                                          day.world.config.fiscal_year_start_month)

    def load(self, orders: list[dict[str, Any]], product_ids: set[int]) -> None:
        """Everything ``bill`` will need for these orders, read before any document is written."""
        day = self.day
        missing = sorted(product_ids - self.products.keys())
        if missing:
            for row in day.rows(f"""
                    SELECT p.product_id, p.hsn_code, p.business_unit_id, p.weight_kg, u.gst_uqc FROM products p
                    JOIN product_units_of_measure u ON u.unit_of_measure_id = p.base_unit_of_measure_id
                    WHERE p.product_id IN ({", ".join(map(str, missing))})"""):
                self.products[row["product_id"]] = row
        preload_rates(day, product_ids, day.day)
        customers = sorted({o["customer_id"] for o in orders} - self.customers.keys())
        if customers:
            for row in day.rows(f"SELECT customer_id, customer_name, gstin FROM customers "
                                f"WHERE customer_id IN ({', '.join(map(str, customers))})"):
                self.customers[row["customer_id"]] = row
        order_ids = sorted({o["order_id"] for o in orders} - self.prepayments.keys())
        if order_ids:
            in_orders = ", ".join(map(str, order_ids))
            for row in day.rows(f"""
                    SELECT p.order_id, a.accounts_receivable_id, a.customer_payment_id, a.original_amount, a.open_amount,
                           a.item_status
                    FROM customer_payments p JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
                    WHERE p.order_id IN ({in_orders}) AND p.payment_type = 'ORDER_PREPAYMENT'
                      AND p.payment_status = 'SUCCESS'"""):
                self.prepayments[row["order_id"]] = row
            self.shipping_billed |= {r["order_id"] for r in day.rows(f"""
                SELECT DISTINCT i.order_id FROM invoices i JOIN invoice_line_items l ON l.invoice_id = i.invoice_id
                WHERE i.order_id IN ({in_orders}) AND i.invoice_status = 'ISSUED' AND l.line_type = 'SHIPPING_CHARGE'""")}

    def remaining_prepayment(self, order_id: int) -> Decimal | None:
        item = self.prepayments.get(order_id)
        return -item["open_amount"] if item else None

    # ------------------------------------------------------------------------------------------ the document

    def bill(self, order: dict[str, Any], warehouse_id: int, place_of_supply_state_id: int,
             shipping_address_id: int | None, issued_at: datetime, lines: list[BillLine], *,
             shipment_id: int | None = None, completes_order: bool = False) -> Bill:
        day = self.day
        warehouse = self.warehouses[warehouse_id]
        registration = self.registrations[warehouse["company_gst_registration_id"]]
        inter_state = place_of_supply_state_id != registration["state_id"]
        customer = self.customers[order["customer_id"]]
        invoice_id = day.ids.next("invoices")
        number = day.numbers.next(day.conn, "TAX_INVOICE", day.day,
                                  Registration(registration["company_gst_registration_id"], registration["state_code"]))
        rows: list[dict[str, Any]] = []
        for line in lines:
            ol = line.order_line
            rate = rate_on(day, ol["product_id"], day.day)
            if ol["inclusive_unit_price"] is not None:
                priced = price_inclusive(line.quantity, ol["inclusive_unit_price"], ol["discount_percent"], rate)
            else:
                priced = price_exclusive(line.quantity, ol["unit_price"], ol["discount_percent"], rate)
            line.invoice_line_item_id = day.ids.next("invoice_line_items")
            rows.append({
                "invoice_line_item_id": line.invoice_line_item_id, "line_type": "PRODUCT",
                "order_line_item_id": ol["order_line_item_id"], "shipment_line_item_id": line.shipment_line_item_id,
                "product_id": ol["product_id"], "hsn_code": self.products[ol["product_id"]]["hsn_code"],
                "quantity": priced.quantity, "unit_price": priced.unit_price,
                "inclusive_unit_price": priced.inclusive_unit_price, "discount_percent": priced.discount_percent,
                "taxable_amount": priced.taxable_amount, "gst_rate": priced.gst_rate})
        if order["shipping_charge_amount"] > 0 and order["order_id"] not in self.shipping_billed:
            top_rate = max(r["gst_rate"] for r in rows)
            priced = price_exclusive(1, order["shipping_charge_amount"], 0, top_rate)
            rows.append({
                "invoice_line_item_id": day.ids.next("invoice_line_items"), "line_type": "SHIPPING_CHARGE",
                "order_line_item_id": None, "shipment_line_item_id": None, "product_id": None,
                "hsn_code": SHIPPING_SAC, "quantity": priced.quantity, "unit_price": priced.unit_price,
                "inclusive_unit_price": None, "discount_percent": priced.discount_percent,
                "taxable_amount": priced.taxable_amount, "gst_rate": priced.gst_rate})
            self.shipping_billed.add(order["order_id"])
        for row in rows:
            split = split_gst(row["taxable_amount"], row["gst_rate"], inter_state)
            row.update(cgst_amount=split.cgst, sgst_amount=split.sgst, igst_amount=split.igst,
                       line_total=row["taxable_amount"] + split.total)
        sums = document_totals(tax_line(r["taxable_amount"], r["gst_rate"], inter_state) for r in rows)
        exact = sums.taxable_amount + sums.cgst_amount + sums.sgst_amount + sums.igst_amount
        grand, round_off = sums.total_amount, sums.round_off_amount
        prepaid = self.remaining_prepayment(order["order_id"])
        if completes_order and prepaid is not None and prepaid > 0 and MIN_COMPLETING_ROUND_OFF <= prepaid - exact <= MAX_ROUND_OFF \
                and money(prepaid) == prepaid and prepaid == prepaid.to_integral_value():
            grand, round_off = prepaid, prepaid - exact
        term = self.terms[order["payment_term_id"]]
        gstin = customer["gstin"]
        day.writer.insert("invoices", {
            "invoice_id": invoice_id, "invoice_number": number, "order_id": order["order_id"],
            "shipment_id": shipment_id, "customer_id": order["customer_id"],
            "company_gst_registration_id": registration["company_gst_registration_id"], "warehouse_id": warehouse_id,
            "invoice_date": day.day, "issued_at": issued_at, "customer_name": customer["customer_name"],
            "customer_gstin": gstin, "billing_address_id": order["billing_address_id"],
            "shipping_address_id": shipping_address_id, "place_of_supply_state_id": place_of_supply_state_id,
            "is_inter_state": inter_state, "payment_term_id": order["payment_term_id"],
            "due_date": day.day + timedelta(days=term["due_days"]), "taxable_amount": sums.taxable_amount,
            "cgst_amount": sums.cgst_amount, "sgst_amount": sums.sgst_amount, "igst_amount": sums.igst_amount,
            "round_off_amount": round_off, "total_amount": grand,
            "irn": irn(registration["gstin"], self.fy_label, "INV", number) if gstin else None,
            "irn_generated_at": issued_at if gstin else None, "invoice_status": "ISSUED", "cancelled_at": None,
            "cancellation_reason": None, "created_at": issued_at, "updated_at": issued_at,
        })
        section = "B2B" if gstin else ("B2CL" if inter_state and grand > self.b2cl_threshold else "B2CS")
        for number_in_doc, row in enumerate(rows, 1):
            day.writer.insert("invoice_line_items", {
                "invoice_line_item_id": row["invoice_line_item_id"], "invoice_id": invoice_id,
                "line_number": number_in_doc, "line_type": row["line_type"],
                "order_line_item_id": row["order_line_item_id"], "shipment_line_item_id": row["shipment_line_item_id"],
                "product_id": row["product_id"], "hsn_code": row["hsn_code"], "quantity": row["quantity"],
                "unit_price": row["unit_price"], "inclusive_unit_price": row["inclusive_unit_price"],
                "discount_percent": row["discount_percent"], "taxable_amount": row["taxable_amount"],
                "gst_rate": row["gst_rate"], "cgst_amount": row["cgst_amount"], "sgst_amount": row["sgst_amount"],
                "igst_amount": row["igst_amount"], "line_total": row["line_total"],
                "created_at": issued_at, "updated_at": issued_at,
            })
            product = self.products.get(row["product_id"]) if row["product_id"] else None
            day.writer.insert("tax_transactions", {
                "tax_transaction_id": day.ids.next("tax_transactions"),
                "company_gst_registration_id": registration["company_gst_registration_id"], "tax_direction": "OUTPUT",
                "document_type": "SALES_INVOICE", "is_cancellation": False,
                "invoice_line_item_id": row["invoice_line_item_id"], "credit_note_line_item_id": None,
                "supplier_invoice_line_item_id": None, "expense_line_item_id": None, "stock_transfer_line_item_id": None,
                "document_number": number, "document_date": day.day, "counterparty_gstin": gstin,
                "customer_id": order["customer_id"], "supplier_id": None,
                "place_of_supply_state_id": place_of_supply_state_id, "is_inter_state": inter_state,
                "hsn_code": row["hsn_code"], "uqc": product["gst_uqc"] if product else None,
                "quantity": row["quantity"] if product else None, "taxable_value": row["taxable_amount"],
                "gst_rate": row["gst_rate"], "cgst_amount": row["cgst_amount"], "sgst_amount": row["sgst_amount"],
                "igst_amount": row["igst_amount"], "itc_eligibility": "NOT_APPLICABLE", "gstr1_section": section,
                "gst_return_id": None, "created_at": issued_at, "updated_at": issued_at,
            })
        journal_id = self._post(order, warehouse_id, registration["company_gst_registration_id"], invoice_id, number,
                                rows, lines, grand)
        self._receivable(order, invoice_id, grand, day.day + timedelta(days=term["due_days"]), journal_id, issued_at)
        return Bill(invoice_id, number, grand, lines)

    def _post(self, order: dict[str, Any], warehouse_id: int, registration_id: int, invoice_id: int, number: str,
              rows: list[dict[str, Any]], lines: list[BillLine], grand: Decimal) -> int:
        """P06: revenue, output GST, receivable and the cost of the goods in one entry (D52)."""
        book = self.book
        revenue: dict[int, Decimal] = {}
        for row in rows:
            if row["line_type"] == "PRODUCT":
                unit = self.products[row["product_id"]]["business_unit_id"]
                revenue[unit] = revenue.get(unit, ZERO) + row["taxable_amount"]
        cogs: dict[int, Decimal] = {}
        for line in lines:
            unit = self.products[line.order_line["product_id"]]["business_unit_id"]
            cogs[unit] = cogs.get(unit, ZERO) + line.cost
        main_unit = max(sorted(revenue), key=lambda u: revenue[u])
        builder = JournalBuilder(f"Sales invoice {number}")
        builder.debit(book.role("AR_CONTROL"), grand, customer_id=order["customer_id"])
        for unit in sorted(cogs):
            builder.debit(book.cogs(unit), cogs[unit], cost_center_id=self.bu_cc[unit])
        for unit in sorted(revenue):
            builder.credit(book.revenue(unit), revenue[unit], cost_center_id=self.bu_cc[unit])
        shipping = total(r["taxable_amount"] for r in rows if r["line_type"] == "SHIPPING_CHARGE")
        builder.credit(book.role("SHIPPING_INCOME"), shipping, cost_center_id=self.bu_cc[main_unit])
        for component in ("CGST", "SGST", "IGST"):
            builder.credit(book.tax(registration_id, component, "OUTPUT"),
                           total(r[f"{component.lower()}_amount"] for r in rows))
        builder.credit(book.role("INVENTORY"), total(line.cost for line in lines), warehouse_id=warehouse_id)
        builder.balance_with(book.role("ROUNDING_OFF"), cost_center_id=self.finance_cc)
        return post_journal(self.day, builder.build(), source="SALES_INVOICE", invoice_id=invoice_id)

    def _receivable(self, order: dict[str, Any], invoice_id: int, grand: Decimal, due, journal_id: int,
                    at: datetime) -> None:
        """The invoice's AR item; a prepayment of the order is allocated to it at once (no journal, D49)."""
        day = self.day
        open_amount = grand
        prepayment = self.prepayments.get(order["order_id"])
        if prepayment is not None and prepayment["open_amount"] < 0:
            amount = min(-prepayment["open_amount"], grand)
            open_amount -= amount
            prepayment["open_amount"] += amount
            day.writer.insert("customer_payment_allocations", {
                "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
                "customer_payment_id": prepayment["customer_payment_id"], "credit_note_id": None,
                "invoice_id": invoice_id, "refund_id": None, "allocated_amount": amount, "allocation_date": day.day,
                "allocated_by_employee_id": None, "allocation_status": "ACTIVE", "reversed_at": None,
                "created_at": at, "updated_at": at,
            })
            day.writer.update("accounts_receivable", "accounts_receivable_id", prepayment["accounts_receivable_id"],
                              ar_state(prepayment["original_amount"], prepayment["open_amount"], at))
        state = ar_state(grand, open_amount, at)
        item_id = day.ids.next("accounts_receivable")
        self.invoice_items[invoice_id] = {"accounts_receivable_id": item_id, "original_amount": grand,
                                          "open_amount": open_amount}
        day.writer.insert("accounts_receivable", {
            "accounts_receivable_id": item_id, "customer_id": order["customer_id"],
            "item_type": "INVOICE", "invoice_id": invoice_id, "credit_note_id": None, "customer_payment_id": None,
            "refund_id": None, "document_date": day.day, "due_date": due, "original_amount": grand,
            "open_amount": open_amount, "item_status": state["item_status"], "settled_at": state["settled_at"],
            "journal_entry_id": journal_id, "created_at": at, "updated_at": at,
        })


    def settle_invoice(self, invoice_id: int, amount: Decimal, at: datetime) -> None:
        """A payment of ``amount`` was allocated to an invoice issued today (counter sale)."""
        item = self.invoice_items[invoice_id]
        item["open_amount"] -= amount
        self.day.writer.update("accounts_receivable", "accounts_receivable_id", item["accounts_receivable_id"],
                               ar_state(item["original_amount"], item["open_amount"], at))


def ar_state(original: Decimal, open_amount: Decimal, at: datetime) -> dict[str, Any]:
    """Status columns of an AR item with this open amount (1E: OPEN / PARTIALLY_SETTLED / SETTLED)."""
    if open_amount == 0:
        return {"open_amount": open_amount, "item_status": "SETTLED", "settled_at": at, "updated_at": at}
    status = "OPEN" if open_amount == original else "PARTIALLY_SETTLED"
    return {"open_amount": open_amount, "item_status": status, "settled_at": None, "updated_at": at}


__all__ = ["Bill", "BillLine", "GstError", "Invoicer", "ar_state"]
