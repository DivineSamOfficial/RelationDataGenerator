"""Bank statements, settlements and reconciliations (1E §4, §6 P10/P21; D53, D63; step 3.14a).

The bank's view of the money, one ``bank_transactions`` row per statement line (and petty-cash book line), written
on the day the bank books it, with ``balance_after`` running per account in (statement date, id) order:

* **Document lines** (no journal of their own, the document posted already): direct receipts (NEFT / RTGS / IMPS
  on the payment date, cheques on clearance; a bounced cheque shows as a deposit and its reversal, net nothing),
  store cash into the store's float, supplier payments, expense payments (petty cash from the float), refunds.
* **Gateway settlements** (P10, D53): per collections account × method × settlement day, the gross of the day's
  settled UPI / card / net-banking receipts less their gateway fees: Dr Bank (net), Dr Payment Gateway Charges
  (Finance cost centre), Cr Payment Clearing (gross); each payment gets ``settlement_bank_transaction_id`` (D63,
  late link D86). **COD remittances** likewise per carrier and remittance day. Before a payment the PAYMENTS
  account can't cover, treasury funds it from COLLECTIONS (or draws on the cash-credit GENERAL account). A clearing receipt reversed after it
  was settled (chargeback, refund of a cancelled order) is taken back by the gateway: a REVERSAL debit line, Dr
  Payment Clearing / Cr Bank.
* **Bank-originated items** (P21): monthly bank charges per current / cash-credit account, quarterly interest on
  the collections balance, and petty-cash management between the PAYMENTS account and each warehouse float
  (INTERNAL_TRANSFER pairs, ``counter_bank_transaction_id`` both ways): a top-up before a float would go negative
  and whenever it ends a day below its minimum; a store float above its ceiling deposits the excess in the
  COLLECTIONS account.
* **Reconciliation** (monthly, on the configured working day after the month end, before the period closes): per
  account, the statement closing balance against the bank GL account's balance at the period end; lines of the
  month get the reconciliation id. Clean mode reconciles to a zero difference.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO, money
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext

CLEARING_METHODS = ("UPI", "CARD", "NET_BANKING")


class _Statement:
    """Running statement balances per bank account (rebuilt from the database at the first use of a run, D66)."""

    def __init__(self, day: DayContext) -> None:
        self.accounts = {r["bank_account_id"]: r for r in day.rows("""
            SELECT bank_account_id, account_purpose, account_type, warehouse_id, account_status FROM bank_accounts
            ORDER BY bank_account_id""")}
        book = account_book(day)
        self.balance: dict[int, Decimal] = {}
        last = {r["bank_account_id"]: r["balance_after"] for r in day.rows("""
            SELECT t.bank_account_id, t.balance_after FROM bank_transactions t
            JOIN (SELECT bank_account_id, MAX(bank_transaction_id) AS last_id FROM bank_transactions
                  GROUP BY bank_account_id) m ON m.last_id = t.bank_transaction_id""")}
        opening = {r["account_id"]: r["amount"] for r in day.rows("""
            SELECT l.account_id, SUM(l.debit_amount - l.credit_amount) AS amount FROM journal_entry_lines l
            JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
            WHERE j.journal_source = 'OPENING_BALANCE' GROUP BY l.account_id""")}
        for account_id in self.accounts:
            self.balance[account_id] = last.get(account_id, opening.get(book.bank(account_id), ZERO))


def _statement(day: DayContext) -> _Statement:
    if "bank_statement" not in day.world.refs:
        day.world.refs["bank_statement"] = _Statement(day)
    return day.world.refs["bank_statement"]


class _Day:
    """Today's statement lines, written in order (ids and balances follow the order of creation)."""

    def __init__(self, day: DayContext) -> None:
        self.day = day
        self.statement = _statement(day)
        self.book = account_book(day)
        self.finance_cc = day.rows("SELECT c.cost_center_id FROM cost_centers c JOIN departments d USING (department_id) "
                                   "WHERE d.department_code = 'FIN'")[0]["cost_center_id"]
        purposes = defaultdict(list)
        for account_id, account in sorted(self.statement.accounts.items()):
            if account["account_status"] == "ACTIVE":
                purposes[account["account_purpose"]].append(account_id)
        self.payments_account = purposes["PAYMENTS"][0]
        self.collections_account = purposes["COLLECTIONS"][0]
        self.general_account = (purposes.get("GENERAL") or purposes["COLLECTIONS"])[0]
        self.floats = {a["warehouse_id"]: i for i, a in self.statement.accounts.items() if a["account_purpose"] == "PETTY_CASH"}
        self.float_type = {r["warehouse_id"]: r["warehouse_type"] for r in day.rows(
            "SELECT warehouse_id, warehouse_type FROM warehouses")}

    def line(self, account_id: int, kind: str, narration: str, amount: Decimal, *, inflow: bool,
             reference: str | None = None, value_date: date | None = None, **links: Any) -> int:
        settings = self.day.world.config.bank
        if not inflow and account_id in self.floats.values() and self.statement.balance[account_id] < amount:
            self._top_up(account_id, amount - self.statement.balance[account_id])
        elif not inflow and account_id == self.payments_account and self.statement.balance[account_id] < amount:
            self._fund_payments(amount - self.statement.balance[account_id])
        line_id = self.day.ids.next("bank_transactions")
        self.statement.balance[account_id] += amount if inflow else -amount
        at = self.day.at(settings.statement_hour)
        self.day.writer.insert("bank_transactions", {
            "bank_transaction_id": line_id, "bank_account_id": account_id, "statement_date": self.day.day,
            "value_date": value_date or self.day.day, "transaction_type": kind, "narration": narration[:200],
            "bank_reference": reference, "credit_amount": amount if inflow else ZERO,
            "debit_amount": ZERO if inflow else amount, "balance_after": self.statement.balance[account_id],
            "customer_payment_id": links.get("customer_payment_id"), "supplier_payment_id": links.get("supplier_payment_id"),
            "expense_payment_id": links.get("expense_payment_id"), "refund_id": links.get("refund_id"),
            "gst_return_id": links.get("gst_return_id"), "counter_bank_transaction_id": None,
            "shipping_carrier_id": links.get("shipping_carrier_id"), "bank_reconciliation_id": None,
            "created_at": at, "updated_at": at,
        })
        return line_id

    def transfer(self, source: int, target: int, amount: Decimal, narration: str) -> None:
        """INTERNAL_TRANSFER pair (P21 on the outgoing leg: Dr target bank / Cr source bank)."""
        settings = self.day.world.config.bank
        if source == self.payments_account and self.statement.balance[source] < amount:
            self._fund_payments(amount - self.statement.balance[source])
        at = self.day.at(settings.statement_hour)
        out_id, in_id = self.day.ids.next("bank_transactions"), self.day.ids.next("bank_transactions")
        for line_id, account_id, inflow in ((out_id, source, False), (in_id, target, True)):
            self.statement.balance[account_id] += amount if inflow else -amount
            self.day.writer.insert("bank_transactions", {
                "bank_transaction_id": line_id, "bank_account_id": account_id, "statement_date": self.day.day,
                "value_date": self.day.day, "transaction_type": "INTERNAL_TRANSFER", "narration": narration[:200],
                "bank_reference": f"XIT{self.day.day:%y%m%d}{out_id:08d}", "credit_amount": amount if inflow else ZERO,
                "debit_amount": ZERO if inflow else amount, "balance_after": self.statement.balance[account_id],
                "customer_payment_id": None, "supplier_payment_id": None, "expense_payment_id": None, "refund_id": None,
                "gst_return_id": None, "counter_bank_transaction_id": None, "shipping_carrier_id": None,
                "bank_reconciliation_id": None, "created_at": at, "updated_at": at,
            })
        # The pair points at each other: inserted NULL, linked by UPDATE in the same transaction (D86 pattern).
        self.day.writer.update("bank_transactions", "bank_transaction_id", out_id, {"counter_bank_transaction_id": in_id})
        self.day.writer.update("bank_transactions", "bank_transaction_id", in_id, {"counter_bank_transaction_id": out_id})
        journal = (JournalBuilder(narration).debit(self.book.bank(target), amount)
                   .credit(self.book.bank(source), amount).build())
        post_journal(self.day, journal, source="INTERNAL_TRANSFER", bank_transaction_id=out_id)

    def _fund_payments(self, shortfall: Decimal) -> None:
        """Treasury moves money into the PAYMENTS account before a payment it can't cover: from COLLECTIONS when
        it has the funds, else drawn on the cash-credit (GENERAL) account, an overdraft facility."""
        need = money(shortfall + Decimal(self.day.world.config.bank.payments_buffer))
        source = self.collections_account
        if self.statement.balance[source] - need < Decimal(self.day.world.config.bank.collections_minimum):
            source = self.general_account
        self.transfer(source, self.payments_account, need, "Funds transfer to payments account")

    def _top_up(self, float_account: int, shortfall: Decimal) -> None:
        """Before a payment the float can't cover: enough to pay it and leave the float at its top-up level."""
        settings = self.day.world.config.bank
        warehouse_id = self.statement.accounts[float_account]["warehouse_id"]
        level = settings.petty_float[self.float_type[warehouse_id]]
        self.transfer(self.payments_account, float_account, money(shortfall + Decimal(level["top_up_to"])),
                      f"Petty cash top-up W{warehouse_id}")


# ------------------------------------------------------------------------------------------ document lines


def _document_lines(day: DayContext, today: _Day) -> None:
    """Statement lines of today's completed payments, receipts and refunds (posted by their documents)."""
    for row in day.rows("""
            SELECT p.customer_payment_id, p.payment_number, p.bank_account_id, p.amount, p.payment_status,
                   p.payment_type, m.method_code, p.payment_date, c.customer_name, p.completed_at
            FROM customer_payments p JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
            JOIN customers c ON c.customer_id = p.customer_id
            WHERE m.method_code NOT IN ('UPI', 'CARD', 'NET_BANKING') AND p.payment_type <> 'COD_COLLECTION'
              AND ((p.payment_status IN ('SUCCESS', 'REVERSED') AND DATE(p.completed_at) = :d)
                   OR (p.payment_status = 'FAILED' AND m.method_code = 'CHEQUE' AND DATE(p.completed_at) = :d))
            ORDER BY p.customer_payment_id""", d=day.day):
        name = (row["customer_name"] or "")[:40].upper()
        if row["method_code"] == "CHEQUE":
            today.line(row["bank_account_id"], "CHEQUE_DEPOSIT", f"CHQ DEP {row['payment_number']} {name}", row["amount"],
                       inflow=True, reference=row["payment_number"][-12:], value_date=max(row["payment_date"], day.day - timedelta(days=3)),
                       customer_payment_id=row["customer_payment_id"])
            if row["payment_status"] == "FAILED":
                today.line(row["bank_account_id"], "REVERSAL", f"CHQ RETURNED {row['payment_number']} {name}",
                           row["amount"], inflow=False, reference=row["payment_number"][-12:],
                           customer_payment_id=row["customer_payment_id"])
        else:
            kind = "CASH DEPOSIT" if row["method_code"] == "CASH" else f"{row['method_code']} CR"
            today.line(row["bank_account_id"], "CUSTOMER_RECEIPT", f"{kind} {row['payment_number']} {name}",
                       row["amount"], inflow=True, reference=row["payment_number"][-12:],
                       customer_payment_id=row["customer_payment_id"])
    for row in day.rows("""
            SELECT p.supplier_payment_id, p.payment_number, p.bank_account_id, p.amount, p.bank_reference, s.supplier_name
            FROM supplier_payments p JOIN suppliers s ON s.supplier_id = p.supplier_id
            WHERE p.payment_status = 'COMPLETED' AND p.payment_date = :d ORDER BY p.supplier_payment_id""", d=day.day):
        today.line(row["bank_account_id"], "SUPPLIER_PAYMENT", f"NEFT DR {row['payment_number']} {row['supplier_name'][:40].upper()}",
                   row["amount"], inflow=False, reference=row["bank_reference"], supplier_payment_id=row["supplier_payment_id"])
    for row in day.rows("""
            SELECT p.expense_payment_id, p.payment_number, p.bank_account_id, p.amount, p.bank_reference
            FROM expense_payments p WHERE p.payment_status = 'COMPLETED' AND p.payment_date = :d
            ORDER BY p.expense_payment_id""", d=day.day):
        today.line(row["bank_account_id"], "EXPENSE_PAYMENT", f"DR {row['payment_number']}", row["amount"], inflow=False,
                   reference=row["bank_reference"], expense_payment_id=row["expense_payment_id"])
    for row in day.rows("""
            SELECT r.refund_id, r.refund_number, r.bank_account_id, r.refund_amount, r.bank_reference
            FROM refunds r WHERE r.refund_status = 'COMPLETED' AND DATE(r.processed_at) = :d ORDER BY r.refund_id""",
            d=day.day):
        today.line(row["bank_account_id"], "REFUND", f"REFUND {row['refund_number']}", row["refund_amount"], inflow=False,
                   reference=row["bank_reference"], refund_id=row["refund_id"])


# ------------------------------------------------------------------------------------------ settlements


def _settlements(day: DayContext, today: _Day) -> None:
    """Gateway batches per account × method and COD remittances per carrier, for receipts settling today (P10)."""
    batches: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in day.rows("""
            SELECT p.customer_payment_id, p.bank_account_id, p.amount, p.gateway_fee_amount, m.method_code,
                   p.collected_by_shipping_carrier_id, p.payment_type, p.payment_date
            FROM customer_payments p JOIN payment_methods m ON m.payment_method_id = p.payment_method_id
            WHERE p.payment_status IN ('SUCCESS', 'REVERSED') AND p.settlement_bank_transaction_id IS NULL
              AND p.settlement_date <= :d
              AND (m.method_code IN ('UPI', 'CARD', 'NET_BANKING') OR p.payment_type = 'COD_COLLECTION')
              AND (p.payment_status = 'SUCCESS' OR p.reversed_at >= p.settlement_date)
            ORDER BY p.customer_payment_id""", d=day.day):
        if row["payment_type"] == "COD_COLLECTION":
            batches[("COD", row["bank_account_id"], row["collected_by_shipping_carrier_id"])].append(row)
        else:
            batches[("GATEWAY", row["bank_account_id"], row["method_code"])].append(row)
    for (kind, account_id, key), rows in sorted(batches.items(), key=lambda b: (b[0][0], b[0][1], str(b[0][2]))):
        gross = sum((r["amount"] for r in rows), ZERO)
        fees = sum((r["gateway_fee_amount"] for r in rows), ZERO)
        if kind == "GATEWAY":
            line_id = today.line(account_id, "GATEWAY_SETTLEMENT", f"{key} SETTLEMENT {day.day:%d%m%y} {len(rows)} TXNS",
                                 gross - fees, inflow=True, reference=f"XSET{day.day:%y%m%d}{key[:3]}{account_id}",
                                 )
        else:
            line_id = today.line(account_id, "COD_REMITTANCE", f"COD REMITTANCE CARRIER {key} {len(rows)} PARCELS",
                                 gross - fees, inflow=True, reference=f"XCOD{day.day:%y%m%d}{key}",
                                 shipping_carrier_id=key)
        for row in rows:
            day.writer.update("customer_payments", "customer_payment_id", row["customer_payment_id"],
                              {"settlement_bank_transaction_id": line_id})
        builder = (JournalBuilder(f"{'Gateway settlement' if kind == 'GATEWAY' else 'COD remittance'} {day.day}")
                   .debit(today.book.bank(account_id), gross - fees))
        builder.debit(today.book.role("PAYMENT_GATEWAY_CHARGES"), fees, cost_center_id=today.finance_cc)
        builder.credit(today.book.role("PAYMENT_CLEARING"), gross)
        post_journal(day, builder.build(), source="BANK_SETTLEMENT", bank_transaction_id=line_id)


def _clawbacks(day: DayContext, today: _Day) -> None:
    """A settled clearing receipt reversed today (chargeback, refund to source) is taken back from the bank."""
    for row in day.rows("""
            SELECT p.customer_payment_id, p.payment_number, p.bank_account_id, p.amount, p.reversal_reason
            FROM customer_payments p
            WHERE p.payment_status = 'REVERSED' AND p.settlement_bank_transaction_id IS NOT NULL
              AND DATE(p.reversed_at) <= :d
              AND NOT EXISTS (SELECT 1 FROM bank_transactions t WHERE t.customer_payment_id = p.customer_payment_id
                                AND t.transaction_type = 'REVERSAL')
            ORDER BY p.customer_payment_id""", d=day.day):
        line_id = today.line(row["bank_account_id"], "REVERSAL",
                             f"GATEWAY DEBIT {row['payment_number']} {(row['reversal_reason'] or '').upper()}",
                             row["amount"], inflow=False, reference=row["payment_number"][-12:],
                             customer_payment_id=row["customer_payment_id"])
        journal = (JournalBuilder(f"Gateway clawback {row['payment_number']}")
                   .debit(today.book.role("PAYMENT_CLEARING"), row["amount"])
                   .credit(today.book.bank(row["bank_account_id"]), row["amount"]).build())
        post_journal(day, journal, source="BANK_SETTLEMENT", bank_transaction_id=line_id)


# ------------------------------------------------------------------------------------------ bank-originated items


def _last_working_day_of_month(day: DayContext) -> bool:
    last = date(day.day.year, day.day.month, calendar.monthrange(day.day.year, day.day.month)[1])
    probe = last
    while not day.world.config.daily.is_working_day(probe):
        probe -= timedelta(days=1)
    return probe == day.day


def _charges_and_interest(day: DayContext, today: _Day) -> None:
    settings = day.world.config.bank
    if not _last_working_day_of_month(day):
        return
    for account_id, account in sorted(today.statement.accounts.items()):
        band = settings.monthly_charges.get(account["account_type"])
        if account["account_status"] != "ACTIVE" or not band:
            continue
        rng = day.streams.stream(day.day, f"bank.charges.{account_id}")
        amount = money(Decimal(rng.randint(*band)))
        line_id = today.line(account_id, "BANK_CHARGES", f"SERVICE CHARGES {day.day:%b %Y}".upper(), amount,
                             inflow=False, reference=f"XCHG{day.day:%y%m}{account_id}")
        journal = (JournalBuilder(f"Bank charges {day.day:%b %Y}")
                   .debit(today.book.role("BANK_CHARGES"), amount, cost_center_id=today.finance_cc)
                   .credit(today.book.bank(account_id), amount).build())
        post_journal(day, journal, source="BANK_CHARGES", bank_transaction_id=line_id)
    if day.day.month % 3 == 0:
        account_id = today.collections_account
        balance = today.statement.balance[account_id]
        amount = money(balance * Decimal(str(settings.interest_rate)) / 4) if balance > 0 else ZERO
        if amount > 0:
            line_id = today.line(account_id, "INTEREST_CREDIT", f"INTEREST CREDIT Q{(day.day.month - 1) // 3 + 1}",
                                 amount, inflow=True, reference=f"XINT{day.day:%y%m}{account_id}")
            journal = (JournalBuilder(f"Interest {day.day:%b %Y}").debit(today.book.bank(account_id), amount)
                       .credit(today.book.role("INTEREST_INCOME"), amount, cost_center_id=today.finance_cc).build())
            post_journal(day, journal, source="INTEREST", bank_transaction_id=line_id)


def _floats(day: DayContext, today: _Day) -> None:
    """End of day: top up floats below their minimum; stores deposit cash above their ceiling."""
    settings = day.world.config.bank
    for warehouse_id, account_id in sorted(today.floats.items()):
        level = settings.petty_float[today.float_type[warehouse_id]]
        balance = today.statement.balance[account_id]
        if balance < level["min"]:
            today.transfer(today.payments_account, account_id, money(Decimal(level["top_up_to"]) - balance),
                           f"Petty cash top-up W{warehouse_id}")
        elif level.get("deposit_above") and balance > level["deposit_above"] and day.is_working_day:
            today.transfer(account_id, today.collections_account, money(balance - Decimal(level["top_up_to"])),
                           f"Cash deposit W{warehouse_id}")


# ------------------------------------------------------------------------------------------ reconciliation


def _reconcile(day: DayContext, today: _Day) -> None:
    settings = day.world.config.bank
    first = day.day.replace(day=1)
    probe, count = first, 0
    while True:
        if day.world.config.daily.is_working_day(probe):
            count += 1
            if count == settings.reconciliation_working_day:
                break
        probe += timedelta(days=1)
    if probe != day.day:
        return
    period_end = first - timedelta(days=1)
    period = day.rows("SELECT accounting_period_id FROM accounting_periods WHERE :d BETWEEN start_date AND end_date",
                      d=period_end)
    if not period or period_end < day.world.config.start_date:
        return
    period_id = period[0]["accounting_period_id"]
    staff = day.rows("""SELECT e.employee_id FROM employees e JOIN departments d USING (department_id)
                        WHERE d.department_code = 'FIN' AND e.employment_status = 'ACTIVE' ORDER BY e.employee_id""")
    by = staff[min(1, len(staff) - 1)]["employee_id"]
    at = day.at(17)
    for account_id, account in sorted(today.statement.accounts.items()):
        statement = day.rows("""SELECT balance_after FROM bank_transactions WHERE bank_account_id = :a AND statement_date <= :e
                                ORDER BY statement_date DESC, bank_transaction_id DESC LIMIT 1""", a=account_id, e=period_end)
        book_balance = day.rows("""SELECT COALESCE(SUM(l.debit_amount - l.credit_amount), 0) AS b FROM journal_entry_lines l
                                   JOIN journal_entries j ON j.journal_entry_id = l.journal_entry_id
                                   WHERE l.account_id = :g AND j.posting_date <= :e""",
                                g=today.book.bank(account_id), e=period_end)[0]["b"]
        statement_balance = statement[0]["balance_after"] if statement else book_balance
        difference = book_balance - statement_balance
        reconciliation_id = day.ids.next("bank_reconciliations")
        status = "RECONCILED" if difference == 0 else "EXCEPTION"
        day.writer.insert("bank_reconciliations", {
            "bank_reconciliation_id": reconciliation_id, "bank_account_id": account_id,
            "accounting_period_id": period_id, "statement_closing_balance": statement_balance,
            "book_closing_balance": book_balance, "deposits_in_transit_amount": ZERO,
            "unpresented_payments_amount": ZERO, "unrecorded_bank_items_amount": ZERO,
            "difference_amount": difference, "reconciliation_status": status,
            "reconciled_by_employee_id": by if status == "RECONCILED" else None,
            "reconciled_at": at if status == "RECONCILED" else None, "created_at": at, "updated_at": at,
        })
        day.execute("""UPDATE bank_transactions SET bank_reconciliation_id = :r, updated_at = GREATEST(updated_at, :t)
                       WHERE bank_account_id = :a AND statement_date BETWEEN :s AND :e AND bank_reconciliation_id IS NULL""",
                    {"r": reconciliation_id, "t": at, "a": account_id, "s": period_end.replace(day=1), "e": period_end})


# ------------------------------------------------------------------------------------------ the day step


def bank_step(day: DayContext) -> None:
    """Day step 11 (1G §2.2): statement lines, settlements, charges, floats; reconciliation after the month end."""
    today = _Day(day)
    _reconcile(day, today)
    _document_lines(day, today)
    _settlements(day, today)
    _clawbacks(day, today)
    _charges_and_interest(day, today)
    _floats(day, today)
