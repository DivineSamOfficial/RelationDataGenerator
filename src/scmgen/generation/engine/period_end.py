"""Period-end tasks (1E §3 ``gl_period_balances``, §6 P23; D54; step 3.14c).

* **Depreciation** (P23, journal type RECURRING) on the last day of every month: straight-line on each fixed-asset
  account's cost (its opening balance; v1 buys no new assets) at the account's yearly rate in
  ``period_end.depreciation_rates``, Dr Depreciation (CORP cost centre) / Cr Accumulated Depreciation, until the
  accumulated depreciation reaches the cost.
* **Period balances** (``gl_period_balances``): when a period closes (the periods step, on the close day of the next
  month), its rows are rebuilt from the journal lines posted into it, one per postable account: opening = the
  previous period's closing for balance-sheet accounts; P&L accounts start each financial year at zero, and
  Retained Earnings' opening in the first period of a year absorbs the previous year's net P&L (D54, a virtual
  close: no closing journal). ``rebuild_period_balances`` also serves a full rebuild on demand. IDs come from the run's allocator (D20).
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.money import ZERO, money
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext


# ------------------------------------------------------------------------------------------ depreciation


def depreciation(day: DayContext) -> None:
    if day.day.day != calendar.monthrange(day.day.year, day.day.month)[1]:
        return
    rates = day.world.config.period_end.depreciation_rates
    book = account_book(day)
    accumulated_account = book.role("ACCUMULATED_DEPRECIATION")
    balances = {r["account_code"]: r for r in day.rows("""
        SELECT a.account_id, a.account_code,
               COALESCE((SELECT SUM(l.debit_amount - l.credit_amount) FROM journal_entry_lines l
                         JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                         WHERE l.account_id = a.account_id AND j.journal_source = 'OPENING_BALANCE'), 0) AS cost
        FROM chart_of_accounts a WHERE a.account_code IN :codes""".replace(
            ":codes", "(" + ", ".join(f"'{c}'" for c in sorted(rates)) + ")"))}
    total_cost = sum((b["cost"] for b in balances.values()), ZERO)
    accumulated = -day.rows("""SELECT COALESCE(SUM(debit_amount - credit_amount), 0) AS b FROM journal_entry_lines
                               WHERE account_id = :a""", a=accumulated_account)[0]["b"]
    room = total_cost - accumulated
    charge = ZERO
    for code, row in sorted(balances.items()):
        charge += money(row["cost"] * Decimal(str(rates[code])) / 12)
    charge = min(charge, max(room, ZERO))
    if charge <= 0:
        return
    corp = day.rows("SELECT cost_center_id FROM cost_centers WHERE cost_center_code = 'CORP'")[0]["cost_center_id"]
    journal = (JournalBuilder(f"Depreciation {day.day:%b %Y}")
               .debit(book.role("DEPRECIATION"), charge, cost_center_id=corp)
               .credit(accumulated_account, charge).build())
    post_journal(day, journal, source="DEPRECIATION", journal_type="RECURRING")


def period_end_step(day: DayContext) -> None:
    """Day step 12 (1G §2.2): month-end depreciation (GST returns and bank reconciliations have their own steps)."""
    depreciation(day)


# ------------------------------------------------------------------------------------------ period balances


def rebuild_period_balances(conn: Connection, period_id: int, at, next_id) -> None:
    """(Re)write one period's ``gl_period_balances`` rows from the journal lines posted into it (D54)."""
    period = conn.execute(text("""SELECT accounting_period_id, fiscal_year, period_number, start_date, end_date
                                  FROM accounting_periods WHERE accounting_period_id = :p"""), {"p": period_id}).mappings().one()
    previous = conn.execute(text("""SELECT accounting_period_id, fiscal_year FROM accounting_periods
                                    WHERE end_date < :s ORDER BY end_date DESC LIMIT 1"""),
                            {"s": period["start_date"]}).mappings().first()
    accounts = conn.execute(text("""
        SELECT a.account_id, a.system_role, t.financial_statement FROM chart_of_accounts a
        JOIN account_types t ON t.account_type_id = a.account_type_id WHERE a.is_postable ORDER BY a.account_id""")).mappings().all()
    movements = {r["account_id"]: r for r in conn.execute(text("""
        SELECT l.account_id, SUM(l.debit_amount) AS debit, SUM(l.credit_amount) AS credit FROM journal_entry_lines l
        JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
        WHERE j.accounting_period_id = :p GROUP BY l.account_id"""), {"p": period_id}).mappings()}
    before: dict[int, Decimal] = {}
    if previous is not None:
        before = {r["account_id"]: r["closing_balance"] for r in conn.execute(text(
            "SELECT account_id, closing_balance FROM gl_period_balances WHERE accounting_period_id = :p"),
            {"p": previous["accounting_period_id"]}).mappings()}
    new_year = previous is None or previous["fiscal_year"] != period["fiscal_year"]
    prior_profit = ZERO
    if new_year and previous is not None:
        prior_profit = sum((before.get(a["account_id"], ZERO) for a in accounts
                            if a["financial_statement"] == "PROFIT_AND_LOSS"), ZERO)
    conn.execute(text("DELETE FROM gl_period_balances WHERE accounting_period_id = :p"), {"p": period_id})
    rows = []
    for account in accounts:
        if account["financial_statement"] == "PROFIT_AND_LOSS":
            opening = ZERO if new_year else before.get(account["account_id"], ZERO)
        else:
            opening = before.get(account["account_id"], ZERO)
            if account["system_role"] == "RETAINED_EARNINGS":
                opening += prior_profit           # debit-positive: a profit (credit) lowers it
        moved = movements.get(account["account_id"])
        debit, credit = (moved["debit"], moved["credit"]) if moved else (ZERO, ZERO)
        rows.append({"i": next_id(), "a": account["account_id"], "p": period_id, "o": opening, "d": debit, "c": credit,
                     "x": opening + debit - credit, "t": at})
    conn.execute(text("""
        INSERT INTO gl_period_balances (gl_period_balance_id, account_id, accounting_period_id, opening_balance, period_debit, period_credit,
                                        closing_balance, rebuilt_at, created_at, updated_at)
        VALUES (:i, :a, :p, :o, :d, :c, :x, :t, :t, :t)"""), rows)


def close_period_balances(day: DayContext, period_id: int, at) -> None:
    """Called by the periods step when it closes a period (the writer is flushed first)."""
    day.rows("SELECT 1")                          # flush buffered rows so every journal line of the period is in
    rebuild_period_balances(day.conn, period_id, at, lambda: day.ids.next("gl_period_balances"))
