"""Posting journals from day steps (1E §3, §6; D16, D50, D59, D61, D62).

``post_journal`` takes a balanced ``Journal`` from the builder (unbalanced journals can't exist, D62), finds
the posting period (D59: the event date's period if it is OPEN, else the first day of the earliest open
period), takes the next ``JV`` number (D75), and buffers the header and lines with the source-document FK
(D50: the generated posting key then makes a second posting of the same event impossible).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.domain.journal import AccountBook, BusinessUnitAccounts, Journal
from scmgen.generation.engine.day import DayContext

SOURCE_COLUMNS = {
    "goods_receipt_id", "supplier_invoice_id", "supplier_payment_id", "invoice_id", "credit_note_id",
    "customer_payment_id", "refund_id", "sales_return_id", "shipment_id", "stock_adjustment_id", "stock_transfer_id",
    "defective_stock_id", "expense_id", "expense_payment_id", "bank_transaction_id", "gst_return_id",
}


def load_account_book(conn: Connection) -> AccountBook:
    """Account determination from master data only (D16)."""
    roles = dict(conn.execute(text(
        "SELECT system_role, account_id FROM chart_of_accounts WHERE system_role IS NOT NULL")).all())
    units = {bu: BusinessUnitAccounts(rev, ret, cogs) for bu, rev, ret, cogs in conn.execute(text(
        "SELECT business_unit_id, revenue_account_id, sales_returns_account_id, cogs_account_id FROM business_units"))}
    taxes = {(reg, component, direction): account for reg, component, direction, account in conn.execute(text(
        "SELECT company_gst_registration_id, tax_component, tax_direction, gl_account_id FROM tax_accounts"))}
    expenses = dict(conn.execute(text("SELECT expense_category_id, gl_account_id FROM expense_categories")).all())
    banks = dict(conn.execute(text("SELECT bank_account_id, gl_account_id FROM bank_accounts")).all())
    return AccountBook(roles=roles, business_units=units, tax_accounts=taxes, expense_categories=expenses,
                       bank_accounts=banks)


def account_book(day: DayContext) -> AccountBook:
    """The account book, read once a day (chart of accounts and its links don't change within a day)."""
    if "account_book" not in day.notes:
        day.notes["account_book"] = load_account_book(day.conn)
    return day.notes["account_book"]


def posting_period(day: DayContext, event_date: date) -> tuple[int, date]:
    """D59: (period id, posting date) for an event: its own period if OPEN, else the earliest OPEN period.
    The OPEN periods are read once a day (only the periods step, which runs first, changes them)."""
    if "open_periods" not in day.notes:
        day.notes["open_periods"] = day.rows("SELECT accounting_period_id, start_date, end_date FROM accounting_periods "
                                             "WHERE period_status = 'OPEN' ORDER BY start_date")
    periods = day.notes["open_periods"]
    for period in periods:
        if period["start_date"] <= event_date <= period["end_date"]:
            return period["accounting_period_id"], event_date
    if not periods:
        raise RuntimeError(f"No OPEN accounting period to post an event of {event_date}")
    return periods[0]["accounting_period_id"], max(periods[0]["start_date"], event_date)


def post_journal(day: DayContext, journal: Journal, *, source: str, journal_type: str = "SYSTEM",
                 document_date: date | None = None, created_by: int | None = None,
                 **source_fk: int) -> int:
    """Buffer one posted journal (header + lines). Returns its id."""
    unknown = set(source_fk) - SOURCE_COLUMNS
    if unknown or len(source_fk) > 1:
        raise ValueError(f"A journal links at most one source document, got {sorted(source_fk)}")
    event_date = document_date or day.day
    period_id, posting_date = posting_period(day, event_date)
    journal_id = day.ids.next("journal_entries")
    stamp = day.at(23, 0)                      # posted at the end of the business day that recorded it
    number = day.numbers.next(day.conn, "JV", posting_date)
    header: dict[str, Any] = {column: None for column in sorted(SOURCE_COLUMNS)}
    header.update(source_fk)
    total = journal.total_debit
    day.writer.insert("journal_entries", {
        "journal_entry_id": journal_id, "journal_number": number, "journal_source": source,
        "journal_type": journal_type, "posting_date": posting_date, "document_date": event_date,
        "accounting_period_id": period_id, "description": journal.description[:255], "total_debit": total,
        "total_credit": journal.total_credit, "entry_status": "POSTED", "reverses_journal_entry_id": None,
        **header, "created_by_employee_id": created_by, "generation_run_id": day.run_id,
        "created_at": stamp, "updated_at": stamp,
    })
    for line in journal.lines:
        day.writer.insert("journal_entry_lines", {
            "journal_entry_line_id": day.ids.next("journal_entry_lines"), "journal_entry_id": journal_id,
            "line_number": line.line_number, "account_id": line.account_id, "debit_amount": line.debit_amount,
            "credit_amount": line.credit_amount, "cost_center_id": line.cost_center_id, "customer_id": line.customer_id,
            "supplier_id": line.supplier_id, "employee_id": line.employee_id, "warehouse_id": line.warehouse_id,
            "line_description": line.line_description, "created_at": stamp, "updated_at": stamp,
        })
    return journal_id


