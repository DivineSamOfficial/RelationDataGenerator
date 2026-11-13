"""GST credit notes against an invoice (1D §5 ``credit_notes``; D38, D41, D44, D46, D49; 1E P11; step 3.9b).

``issue_credit_note`` writes one credit note with its lines, the negative OUTPUT tax rows (GSTR-1 ``CDNR`` for a
registered buyer, ``CDNUR`` otherwise), **P11** (Dr Sales Returns per business unit, Dr Shipping Income for a
credited shipping charge, Dr the invoice GSTIN's output GST, Cr AR, round-off) and the AR credit item. The credit
is allocated to the invoice's open amount at once (D49); what is left (the customer had already paid) stays an
open credit until it is refunded (3.11).

The credit note is issued by the invoice's GSTIN, with the invoice's tax type (``is_inter_state``) and each line at
its invoice line's rate. Step 3.9b credits whole invoices (return to origin, D41: every line, the shipping charge
and the same round-off, so the credit equals the invoice total); returns (3.11) will credit received quantities.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from scmgen.core.calendar import fiscal_year, fiscal_year_label
from scmgen.core.money import ZERO, total
from scmgen.domain.identifiers import irn
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.fulfilment.invoicing import Invoicer, ar_state
from scmgen.generation.numbering import Registration

AMOUNTS = ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount")


def load_invoices(day: DayContext, invoice_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Invoices with their lines (product BU, UoM) and AR item, for crediting or cancelling them."""
    if not invoice_ids:
        return {}
    in_ids = ", ".join(map(str, sorted(invoice_ids)))
    invoices = {r["invoice_id"]: {**r, "lines": []} for r in day.rows(f"""
        SELECT i.*, a.accounts_receivable_id, a.original_amount AS item_original, a.open_amount AS item_open,
               j.journal_entry_id AS sales_journal_id
        FROM invoices i JOIN accounts_receivable a ON a.invoice_id = i.invoice_id
        JOIN journal_entries j ON j.invoice_id = i.invoice_id AND j.journal_source = 'SALES_INVOICE'
        WHERE i.invoice_id IN ({in_ids})""")}
    for line in day.rows(f"""
            SELECT il.*, p.business_unit_id, u.gst_uqc FROM invoice_line_items il
            LEFT JOIN products p ON p.product_id = il.product_id
            LEFT JOIN product_units_of_measure u ON u.unit_of_measure_id = p.base_unit_of_measure_id
            WHERE il.invoice_id IN ({in_ids}) ORDER BY il.invoice_id, il.line_number"""):
        invoices[line["invoice_id"]]["lines"].append(line)
    return invoices


def issue_credit_note(day: DayContext, invoicer: Invoicer, invoice: dict[str, Any], reason: str, at: datetime,
                      approved_by: int) -> int:
    """Credit the whole invoice (every line, its shipping charge, the same round-off). Returns the credit note id."""
    registration = invoicer.registrations[invoice["company_gst_registration_id"]]
    note_id = day.ids.next("credit_notes")
    number = day.numbers.next(day.conn, "CREDIT_NOTE", day.day,
                              Registration(registration["company_gst_registration_id"], registration["state_code"]))
    gstin = invoice["customer_gstin"]
    fy = fiscal_year_label(fiscal_year(day.day, day.world.config.fiscal_year_start_month),
                           day.world.config.fiscal_year_start_month)
    grand = invoice["total_amount"]
    day.writer.insert("credit_notes", {
        "credit_note_id": note_id, "credit_note_number": number, "invoice_id": invoice["invoice_id"],
        "customer_id": invoice["customer_id"], "company_gst_registration_id": invoice["company_gst_registration_id"],
        "credit_note_date": day.day, "reason": reason, "sales_return_id": None,
        "is_inter_state": invoice["is_inter_state"], **{k: invoice[k] for k in AMOUNTS},
        "round_off_amount": invoice["round_off_amount"], "total_amount": grand,
        "irn": irn(registration["gstin"], fy, "CRN", number) if gstin else None,
        "irn_generated_at": at if gstin else None, "approved_by_employee_id": approved_by,
        "credit_note_status": "ISSUED", "created_at": at, "updated_at": at,
    })
    section = "CDNR" if gstin else "CDNUR"
    for line in invoice["lines"]:
        line_id = day.ids.next("credit_note_line_items")
        day.writer.insert("credit_note_line_items", {
            "credit_note_line_item_id": line_id, "credit_note_id": note_id, "line_number": line["line_number"],
            "invoice_line_item_id": line["invoice_line_item_id"], "sales_return_line_item_id": None,
            "quantity": line["quantity"], "unit_price": line["unit_price"], "discount_percent": line["discount_percent"],
            "taxable_amount": line["taxable_amount"], "gst_rate": line["gst_rate"], "cgst_amount": line["cgst_amount"],
            "sgst_amount": line["sgst_amount"], "igst_amount": line["igst_amount"], "line_total": line["line_total"],
            "created_at": at, "updated_at": at,
        })
        product = line["line_type"] == "PRODUCT"
        day.writer.insert("tax_transactions", {
            "tax_transaction_id": day.ids.next("tax_transactions"),
            "company_gst_registration_id": invoice["company_gst_registration_id"], "tax_direction": "OUTPUT",
            "document_type": "CREDIT_NOTE", "is_cancellation": False, "invoice_line_item_id": None,
            "credit_note_line_item_id": line_id, "supplier_invoice_line_item_id": None, "expense_line_item_id": None,
            "stock_transfer_line_item_id": None, "document_number": number, "document_date": day.day,
            "counterparty_gstin": gstin, "customer_id": invoice["customer_id"], "supplier_id": None,
            "place_of_supply_state_id": invoice["place_of_supply_state_id"], "is_inter_state": invoice["is_inter_state"],
            "hsn_code": line["hsn_code"], "uqc": line["gst_uqc"] if product else None,
            "quantity": -line["quantity"] if product else None, "taxable_value": -line["taxable_amount"],
            "gst_rate": line["gst_rate"], "cgst_amount": -line["cgst_amount"], "sgst_amount": -line["sgst_amount"],
            "igst_amount": -line["igst_amount"], "itc_eligibility": "NOT_APPLICABLE", "gstr1_section": section,
            "gst_return_id": None, "created_at": at, "updated_at": at,
        })
    journal_id = _post(day, invoicer, invoice, note_id, number, grand)
    _receivable(day, invoice, note_id, grand, journal_id, at)
    return note_id


def _post(day: DayContext, invoicer: Invoicer, invoice: dict[str, Any], note_id: int, number: str,
          grand: Decimal) -> int:
    """P11: the mirror of the invoice's revenue side, on Sales Returns."""
    book = invoicer.book
    returns: dict[int, Decimal] = {}
    for line in invoice["lines"]:
        if line["line_type"] == "PRODUCT":
            returns[line["business_unit_id"]] = returns.get(line["business_unit_id"], ZERO) + line["taxable_amount"]
    main_unit = max(sorted(returns), key=lambda u: returns[u])
    builder = JournalBuilder(f"Credit note {number}")
    for unit in sorted(returns):
        builder.debit(book.sales_returns(unit), returns[unit], cost_center_id=invoicer.bu_cc[unit])
    builder.debit(book.role("SHIPPING_INCOME"), total(l["taxable_amount"] for l in invoice["lines"]
                                                      if l["line_type"] == "SHIPPING_CHARGE"),
                  cost_center_id=invoicer.bu_cc[main_unit])
    for component in ("CGST", "SGST", "IGST"):
        builder.debit(book.tax(invoice["company_gst_registration_id"], component, "OUTPUT"),
                      total(l[f"{component.lower()}_amount"] for l in invoice["lines"]))
    builder.credit(book.role("AR_CONTROL"), grand, customer_id=invoice["customer_id"])
    builder.balance_with(book.role("ROUNDING_OFF"), cost_center_id=invoicer.finance_cc)
    return post_journal(day, builder.build(), source="CREDIT_NOTE", credit_note_id=note_id)


def _receivable(day: DayContext, invoice: dict[str, Any], note_id: int, grand: Decimal, journal_id: int,
                at: datetime) -> None:
    """The credit's AR item, allocated to what is still open on the invoice (D49)."""
    applied = min(grand, invoice["item_open"])
    state = ar_state(-grand, -grand + applied, at)
    day.writer.insert("accounts_receivable", {
        "accounts_receivable_id": day.ids.next("accounts_receivable"), "customer_id": invoice["customer_id"],
        "item_type": "CREDIT_NOTE", "invoice_id": None, "credit_note_id": note_id, "customer_payment_id": None,
        "refund_id": None, "document_date": day.day, "due_date": day.day, "original_amount": -grand,
        "open_amount": state["open_amount"], "item_status": state["item_status"], "settled_at": state["settled_at"],
        "journal_entry_id": journal_id, "created_at": at, "updated_at": at,
    })
    if applied > 0:
        day.writer.insert("customer_payment_allocations", {
            "customer_payment_allocation_id": day.ids.next("customer_payment_allocations"),
            "customer_payment_id": None, "credit_note_id": note_id, "invoice_id": invoice["invoice_id"],
            "refund_id": None, "allocated_amount": applied, "allocation_date": day.day,
            "allocated_by_employee_id": None, "allocation_status": "ACTIVE", "reversed_at": None,
            "created_at": at, "updated_at": at,
        })
        invoice["item_open"] -= applied
        day.writer.update("accounts_receivable", "accounts_receivable_id", invoice["accounts_receivable_id"],
                          ar_state(invoice["item_original"], invoice["item_open"], at))
