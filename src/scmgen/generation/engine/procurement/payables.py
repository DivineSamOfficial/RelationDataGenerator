"""Supplier invoices and payments (1C §3.0, 1E §5-6; D35, D49, D51; step 3.7c).

* **Invoicing.** One bill per GRN with accepted goods: the k-th such GRN of a PO is billed by the PO's k-th
  invoice, so "is this GRN billed?" is read from the documents (D66). Its timing and any price difference
  come from a stream keyed by the GRN (D65). The bill reaches Accounts on a working day; three-way match:
  billed quantity = accepted, price = PO price (MATCHED) or not (PRICE_VARIANCE, approved; the difference
  goes to Purchase Price Variance, D35).
* **Approval** by Finance within the approval limit: AP open item, INPUT tax transactions (registered
  suppliers), P04 Dr GRNI (PO price) ± PPV, Dr Input GST of the bill-to GSTIN, Cr AP; Rounding ±.
* **Weekly payment run.** Approved items due before the next run (some deliberately late, MSME never beyond
  the legal limit) are paid, one payment per supplier, from the PAYMENTS bank account: P05 Dr AP / Cr Bank,
  the payment's AP item and allocations that settle the invoices (allocations post nothing, D49). A failed
  transfer posts nothing and the invoices wait for the next run.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money, product, unit_price
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import approver, max_approval, price_lines, record_status, totals
from scmgen.generation.engine.procurement.sourcing import _dcs
from scmgen.generation.numbering import Registration

PAYMENT_FAILURES = ("Beneficiary account details mismatch", "Beneficiary bank unavailable", "Account frozen by bank")


# ------------------------------------------------------------------------------------------ invoicing


def _bill_plan(day: DayContext, grn: dict[str, Any]) -> tuple[int, int, float | None]:
    """(days after receipt the bill is dated, days it takes to reach Accounts, price factor or None)."""
    settings = day.world.config.procurement
    rng = day.streams.stream(grn["receipt_date"], f"procurement.bill.{grn['goods_receipt_id']}")
    dated, arrives = rng.randint(*settings.invoice_days), rng.randint(*settings.invoice_arrival_days)
    factor = None
    if rng.random() < settings.price_variance_share:
        factor = 1 + rng.choice((-1, 1)) * rng.uniform(*settings.price_variance)
    return dated, arrives, factor


def supplier_invoicing(day: DayContext) -> None:
    """Bills whose arrival date has come are recorded today (Accounts works on working days only)."""
    grns = day.rows("""
        SELECT g.goods_receipt_id, g.purchase_order_id, g.receipt_date,
               ROW_NUMBER() OVER (PARTITION BY g.purchase_order_id ORDER BY g.goods_receipt_id) AS seq,
               COALESCE(b.bills, 0) AS bills
        FROM goods_receipts g
        LEFT JOIN (SELECT purchase_order_id, COUNT(*) AS bills FROM supplier_invoices WHERE invoice_status <> 'CANCELLED'
                   GROUP BY purchase_order_id) b ON b.purchase_order_id = g.purchase_order_id
        WHERE g.grn_status = 'INSPECTED' AND g.receipt_date < :d
          AND g.purchase_order_id IN (SELECT purchase_order_id FROM purchase_order_headers      -- CLOSED = fully billed
                                      WHERE po_status IN ('PARTIALLY_RECEIVED', 'RECEIVED', 'SHORT_CLOSED'))
          AND EXISTS (SELECT 1 FROM goods_receipt_line_items gl JOIN goods_inspections gi
                      ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
                      WHERE gl.goods_receipt_id = g.goods_receipt_id AND gi.quantity_accepted > 0)
        ORDER BY g.goods_receipt_id""", d=day.day)
    # ROW_NUMBER over billable GRNs only: the k-th billable GRN is billed by the k-th bill.
    due = []
    for grn in grns:
        if grn["seq"] != grn["bills"] + 1:
            continue                                   # billed already, or waits for the PO's earlier bill
        dated, arrives, factor = _bill_plan(day, grn)
        if day.day >= grn["receipt_date"] + timedelta(days=dated + arrives):
            # Recorded no earlier than the day after the receipt (the bill reaches Accounts from the dock).
            due.append((grn, min(grn["receipt_date"] + timedelta(days=dated), day.day), factor))
    if due:
        _record_bills(day, due)
    close_completed_pos(day)


def _record_bills(day: DayContext, due: list[tuple[dict[str, Any], date, float | None]]) -> None:
    ids = ", ".join(str(g["goods_receipt_id"]) for g, _, _ in due)
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows(f"""
            SELECT gl.goods_receipt_id, gl.line_number, gi.quantity_accepted, l.purchase_order_line_item_id,
                   l.unit_price, l.discount_percent, l.gst_rate, l.quantity_invoiced
            FROM goods_receipt_line_items gl
            JOIN goods_inspections gi ON gi.goods_receipt_line_item_id = gl.goods_receipt_line_item_id
            JOIN purchase_order_line_items l ON l.purchase_order_line_item_id = gl.purchase_order_line_item_id
            WHERE gl.goods_receipt_id IN ({ids}) AND gi.quantity_accepted > 0
            ORDER BY gl.goods_receipt_id, gl.line_number"""):
        lines[line["goods_receipt_id"]].append(line)
    pos = {r["purchase_order_id"]: r for r in day.rows(f"""
        SELECT h.purchase_order_id, h.supplier_id, h.delivery_warehouse_id, h.is_inter_state, h.po_status,
               t.due_days, s.gstin, s.supplier_code
        FROM purchase_order_headers h JOIN payment_terms t ON t.payment_term_id = h.payment_term_id
        JOIN suppliers s ON s.supplier_id = h.supplier_id
        WHERE h.purchase_order_id IN (SELECT purchase_order_id FROM goods_receipts WHERE goods_receipt_id IN ({ids}))""")}
    for grn, invoice_date, factor in due:
        _record_bill(day, grn, pos[grn["purchase_order_id"]], lines[grn["goods_receipt_id"]], invoice_date, factor)


def _record_bill(day: DayContext, grn: dict[str, Any], po: dict[str, Any], lines: list[dict[str, Any]],
                 invoice_date: date, factor: float | None) -> None:
    warehouse = _dcs(day)[po["delivery_warehouse_id"]]
    items = []
    for line in lines:
        price = line["unit_price"] if factor is None else unit_price(line["unit_price"] * Decimal(str(round(factor, 4))))
        items.append({**line, "invoiced_quantity": line["quantity_accepted"], "unit_price": price,
                      "po_unit_price": line["unit_price"]})
    priced = price_lines(items, bool(po["is_inter_state"]), "invoiced_quantity")
    header = totals(priced, bool(po["is_inter_state"]))
    invoice_id = day.ids.next("supplier_invoices")
    stamp = day.at(11)
    registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
    variance = any(l["unit_price"] != l["po_unit_price"] for l in priced)
    day.writer.insert("supplier_invoices", {
        "supplier_invoice_id": invoice_id,
        "internal_reference": day.numbers.next(day.conn, "PINV", day.day, registration),
        "supplier_id": po["supplier_id"],
        "supplier_invoice_number": f"{po['supplier_code'][-4:]}/{invoice_date:%y%m}/{grn['goods_receipt_id']:06d}",
        "purchase_order_id": po["purchase_order_id"],
        "company_gst_registration_id": warehouse["company_gst_registration_id"], "supplier_gstin": po["gstin"],
        "invoice_date": invoice_date, "received_date": day.day,
        "due_date": invoice_date + timedelta(days=int(po["due_days"])), "is_inter_state": bool(po["is_inter_state"]),
        **header, "match_status": "PRICE_VARIANCE" if variance else "MATCHED", "invoice_status": "RECEIVED",
        "approved_by_employee_id": None, "approved_at": None, "created_at": stamp, "updated_at": stamp,
    })
    for number, line in enumerate(priced, 1):
        day.writer.insert("supplier_invoice_line_items", {
            "supplier_invoice_line_item_id": day.ids.next("supplier_invoice_line_items"),
            "supplier_invoice_id": invoice_id, "line_number": number,
            "purchase_order_line_item_id": line["purchase_order_line_item_id"],
            "invoiced_quantity": line["invoiced_quantity"], "unit_price": line["unit_price"],
            "discount_percent": line["discount_percent"], "taxable_amount": line["taxable_amount"],
            "gst_rate": line["gst_rate"], "cgst_amount": line["cgst_amount"], "sgst_amount": line["sgst_amount"],
            "igst_amount": line["igst_amount"], "line_total": line["line_total"], "po_unit_price": line["po_unit_price"],
            "created_at": stamp, "updated_at": stamp,
        })
        day.writer.update("purchase_order_line_items", "purchase_order_line_item_id", line["purchase_order_line_item_id"], {
            "quantity_invoiced": line["quantity_invoiced"] + line["invoiced_quantity"], "updated_at": stamp})


def close_completed_pos(day: DayContext) -> None:
    """A PO fully received (or short-closed) and fully invoiced is CLOSED, with all its lines. Checked daily:
    a PO can be short-closed after its last bill. Stamped at day end, after today's receipts and short-closes."""
    stamp = day.at(18, 30)
    for po in day.rows("""
            SELECT h.purchase_order_id, h.po_status FROM purchase_order_headers h
            WHERE h.po_status IN ('RECEIVED', 'SHORT_CLOSED')
              AND NOT EXISTS (SELECT 1 FROM purchase_order_line_items l WHERE l.purchase_order_id = h.purchase_order_id
                              AND (l.quantity_open > 0 OR l.quantity_invoiced < l.quantity_accepted))
            ORDER BY h.purchase_order_id"""):
        # The short-close reason stays in the status history (CHECK: only CANCELLED / SHORT_CLOSED carry one).
        day.writer.update("purchase_order_headers", "purchase_order_id", po["purchase_order_id"],
                          {"po_status": "CLOSED", "cancellation_reason": None, "updated_at": stamp})
        day.execute("UPDATE purchase_order_line_items SET line_status = 'CLOSED', updated_at = :t "
                    "WHERE purchase_order_id = :p", {"t": stamp, "p": po["purchase_order_id"]})
        record_status(day, "purchase_order", po["purchase_order_id"], po["po_status"], "CLOSED", stamp)


# ------------------------------------------------------------------------------------------ approval


def invoice_approvals(day: DayContext) -> None:
    """Finance approves received bills after a few days: AP item, INPUT tax rows, P04."""
    settings = day.world.config.procurement
    bills = [b for b in day.rows("""
        SELECT i.*, s.gst_registration_type, w.state_id AS place_of_supply_state_id, h.delivery_warehouse_id
        FROM supplier_invoices i JOIN suppliers s ON s.supplier_id = i.supplier_id
        JOIN purchase_order_headers h ON h.purchase_order_id = i.purchase_order_id
        JOIN warehouses w ON w.warehouse_id = h.delivery_warehouse_id
        WHERE i.invoice_status = 'RECEIVED' AND i.received_date < :d ORDER BY i.supplier_invoice_id""", d=day.day)
        if day.day >= b["received_date"] + timedelta(days=day.streams.stream(
            b["received_date"], f"procurement.approval.{b['supplier_invoice_id']}").randint(*settings.invoice_approval_days))]
    if not bills:
        return
    book = account_book(day)
    finance_cc = day.rows("SELECT c.cost_center_id FROM cost_centers c JOIN departments d USING (department_id) "
                          "WHERE d.department_code = 'FIN'")[0]["cost_center_id"]
    bu_cc = {r["business_unit_id"]: r["cost_center_id"] for r in day.rows(
        "SELECT business_unit_id, cost_center_id FROM cost_centers WHERE business_unit_id IS NOT NULL")}
    ids = ", ".join(str(b["supplier_invoice_id"]) for b in bills)
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows(f"""
            SELECT sl.*, l.discount_percent AS po_discount_percent, p.product_id, p.business_unit_id, p.hsn_code,
                   u.gst_uqc
            FROM supplier_invoice_line_items sl
            JOIN purchase_order_line_items l ON l.purchase_order_line_item_id = sl.purchase_order_line_item_id
            JOIN products p ON p.product_id = l.product_id
            JOIN product_units_of_measure u ON u.unit_of_measure_id = p.base_unit_of_measure_id
            WHERE sl.supplier_invoice_id IN ({ids}) ORDER BY sl.supplier_invoice_id, sl.line_number"""):
        lines[line["supplier_invoice_id"]].append(line)
    for bill in bills:
        approved_at = day.at(15)
        by = approver(day, bill["total_amount"], ("FIN",), day.day)
        day.writer.update("supplier_invoices", "supplier_invoice_id", bill["supplier_invoice_id"], {
            "invoice_status": "APPROVED", "approved_by_employee_id": by, "approved_at": approved_at,
            "updated_at": approved_at})
        registration = bill["company_gst_registration_id"]
        builder = JournalBuilder(f"Supplier invoice {bill['internal_reference']}")
        for line in lines[bill["supplier_invoice_id"]]:
            # GRNI is cleared at the value P03 put in it: accepted (= billed) qty × PO net price.
            po_net = unit_price(line["po_unit_price"] * (1 - line["po_discount_percent"] / 100))
            grni = money(product(line["invoiced_quantity"], po_net))
            builder.debit(book.role("GRNI"), grni)
            builder.debit(book.role("PURCHASE_PRICE_VARIANCE"), line["taxable_amount"] - grni,
                          cost_center_id=bu_cc[line["business_unit_id"]])
        for component in ("CGST", "SGST", "IGST"):
            builder.debit(book.tax(registration, component, "INPUT"), bill[f"{component.lower()}_amount"])
        builder.credit(book.role("AP_CONTROL"), bill["total_amount"], supplier_id=bill["supplier_id"])
        builder.balance_with(book.role("ROUNDING_OFF"), cost_center_id=finance_cc)
        journal_id = post_journal(day, builder.build(), source="SUPPLIER_INVOICE", document_date=bill["invoice_date"],
                                  created_by=by, supplier_invoice_id=bill["supplier_invoice_id"])
        day.writer.insert("accounts_payable", {
            "accounts_payable_id": day.ids.next("accounts_payable"), "supplier_id": bill["supplier_id"],
            "employee_id": None, "item_type": "SUPPLIER_INVOICE", "supplier_invoice_id": bill["supplier_invoice_id"],
            "expense_id": None, "supplier_payment_id": None, "document_date": bill["invoice_date"],
            "due_date": bill["due_date"], "original_amount": bill["total_amount"], "open_amount": bill["total_amount"],
            "item_status": "OPEN", "settled_at": None, "journal_entry_id": journal_id,
            "created_at": approved_at, "updated_at": approved_at,
        })
        if bill["gst_registration_type"] != "REGISTERED":
            continue                                   # no input credit from unregistered / composition suppliers
        for line in lines[bill["supplier_invoice_id"]]:
            day.writer.insert("tax_transactions", {
                "tax_transaction_id": day.ids.next("tax_transactions"), "company_gst_registration_id": registration,
                "tax_direction": "INPUT", "document_type": "SUPPLIER_INVOICE", "is_cancellation": False,
                "invoice_line_item_id": None, "credit_note_line_item_id": None,
                "supplier_invoice_line_item_id": line["supplier_invoice_line_item_id"], "expense_line_item_id": None,
                "stock_transfer_line_item_id": None, "document_number": bill["supplier_invoice_number"],
                "document_date": bill["invoice_date"], "counterparty_gstin": bill["supplier_gstin"], "customer_id": None,
                "supplier_id": bill["supplier_id"], "place_of_supply_state_id": bill["place_of_supply_state_id"],
                "is_inter_state": bool(bill["is_inter_state"]), "hsn_code": line["hsn_code"], "uqc": line["gst_uqc"],
                "quantity": line["invoiced_quantity"], "taxable_value": line["taxable_amount"], "gst_rate": line["gst_rate"],
                "cgst_amount": line["cgst_amount"], "sgst_amount": line["sgst_amount"], "igst_amount": line["igst_amount"],
                "itc_eligibility": "ELIGIBLE", "gstr1_section": None, "gst_return_id": None,
                "created_at": approved_at, "updated_at": approved_at,
            })


# ------------------------------------------------------------------------------------------ payment run


def _pay_by(day: DayContext, item: dict[str, Any]) -> date:
    """The date an invoice is actually paid by: its due date, sometimes weeks later; MSME within the legal limit
    (minus a week, so a failed transfer retried in the next run is still on time)."""
    settings = day.world.config.procurement
    rng = day.streams.stream(item["document_date"], f"procurement.pay.{item['supplier_invoice_id']}")
    pay_by = item["due_date"]
    if rng.random() < settings.late_payment_share:
        pay_by += timedelta(weeks=rng.randint(*settings.late_payment_weeks))
    if item["msme_category"] is not None:
        pay_by = min(pay_by, item["document_date"] + timedelta(days=settings.msme_payment_days - 7))
    return pay_by


def payment_run(day: DayContext) -> None:
    settings = day.world.config.procurement
    if day.day.weekday() != settings.payment_weekday:
        return
    next_run = day.day + timedelta(days=7)
    items = [i for i in day.rows("""
        SELECT a.accounts_payable_id, a.supplier_invoice_id, a.supplier_id, a.document_date, a.due_date, a.open_amount,
               a.original_amount, s.msme_category
        FROM accounts_payable a JOIN suppliers s ON s.supplier_id = a.supplier_id
        WHERE a.item_type = 'SUPPLIER_INVOICE' AND a.item_status IN ('OPEN', 'PARTIALLY_SETTLED')
        ORDER BY a.supplier_id, a.accounts_payable_id""") if _pay_by(day, i) < next_run]
    if not items:
        return
    rng = day.rng("procurement.payments")
    book = account_book(day)
    bank = day.rows("SELECT bank_account_id FROM bank_accounts WHERE account_purpose = 'PAYMENTS' "
                    "AND account_status = 'ACTIVE' ORDER BY bank_account_id LIMIT 1")[0]["bank_account_id"]
    methods = {r["method_code"]: r for r in day.rows("SELECT payment_method_id, method_code, min_amount FROM payment_methods "
                                                     "WHERE method_code IN ('NEFT', 'RTGS')")}
    principal = day.rows("""SELECT r.company_gst_registration_id, s.state_code FROM company_gst_registrations r
                            JOIN states s ON s.state_id = r.state_id WHERE r.is_principal_place""")[0]
    registration = Registration(principal["company_gst_registration_id"], principal["state_code"])
    by_supplier: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_supplier[item["supplier_id"]].append(item)
    ceiling = max_approval(day, ("FIN",), day.day)
    for supplier_id, supplier_items in sorted(by_supplier.items()):
        for open_items in _tranches(supplier_items, ceiling):
            _pay(day, rng, book, bank, methods, registration, supplier_id, open_items)


def _tranches(items: list[dict[str, Any]], ceiling: Decimal) -> list[list[dict[str, Any]]]:
    """Invoices grouped into payments no larger than anyone can approve (one payment per supplier normally)."""
    groups: list[list[dict[str, Any]]] = [[]]
    for item in items:
        if groups[-1] and sum(i["open_amount"] for i in groups[-1]) + item["open_amount"] > ceiling:
            groups.append([])
        groups[-1].append(item)
    return groups


def _pay(day: DayContext, rng: random.Random, book, bank: int, methods: dict[str, Any], registration: Registration,
         supplier_id: int, open_items: list[dict[str, Any]]) -> None:
    settings = day.world.config.procurement
    amount = sum((i["open_amount"] for i in open_items), Decimal(0))
    method = methods["RTGS"] if amount >= methods["RTGS"]["min_amount"] else methods["NEFT"]
    payment_id = day.ids.next("supplier_payments")
    # The run starts after the day's invoice approvals (15:00), so it can pay what was approved today.
    initiated = day.at(16)
    processed = initiated + timedelta(minutes=rng.randint(15, 115))
    failed = rng.random() < settings.payment_failure_share
    approved_by = approver(day, amount, ("FIN",), day.day)
    day.writer.insert("supplier_payments", {
        "supplier_payment_id": payment_id,
        "payment_number": day.numbers.next(day.conn, "SPAY", day.day, registration), "supplier_id": supplier_id,
        "payment_date": day.day, "bank_account_id": bank, "payment_method_id": method["payment_method_id"],
        "amount": amount, "payment_type": "AGAINST_INVOICE",
        "bank_reference": None if failed else f"X{method['method_code'][0]}UTR{day.day:%y%m%d}{payment_id:08d}",
        "payment_status": "FAILED" if failed else "COMPLETED",
        "failure_reason": rng.choice(PAYMENT_FAILURES) if failed else None, "approved_by_employee_id": approved_by,
        "processed_at": processed, "created_at": initiated, "updated_at": processed,
    })
    if failed:
        return                                 # nothing posted; the invoices wait for the next run
    journal = (JournalBuilder(f"Supplier payment {payment_id}")
               .debit(book.role("AP_CONTROL"), amount, supplier_id=supplier_id)
               .credit(book.bank(bank), amount).build())
    journal_id = post_journal(day, journal, source="SUPPLIER_PAYMENT", created_by=approved_by,
                              supplier_payment_id=payment_id)
    day.writer.insert("accounts_payable", {
        "accounts_payable_id": day.ids.next("accounts_payable"), "supplier_id": supplier_id, "employee_id": None,
        "item_type": "SUPPLIER_PAYMENT", "supplier_invoice_id": None, "expense_id": None,
        "supplier_payment_id": payment_id, "document_date": day.day, "due_date": day.day,
        "original_amount": -amount, "open_amount": Decimal(0), "item_status": "SETTLED", "settled_at": processed,
        "journal_entry_id": journal_id, "created_at": processed, "updated_at": processed,
    })
    for item in open_items:
        day.writer.insert("supplier_payment_allocations", {
            "supplier_payment_allocation_id": day.ids.next("supplier_payment_allocations"),
            "supplier_payment_id": payment_id, "supplier_invoice_id": item["supplier_invoice_id"],
            "allocated_amount": item["open_amount"], "allocation_date": day.day, "allocation_status": "ACTIVE",
            "reversed_at": None, "created_at": processed, "updated_at": processed,
        })
        day.writer.update("accounts_payable", "accounts_payable_id", item["accounts_payable_id"], {
            "open_amount": Decimal(0), "item_status": "SETTLED", "settled_at": processed, "updated_at": processed})
