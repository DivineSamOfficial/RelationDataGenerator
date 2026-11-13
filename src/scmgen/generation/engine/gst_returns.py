"""GST returns per GSTIN per month (1E §5 ``gst_returns``; D58; P22; step 3.14b).

* **GSTR-1** (outward supplies), filed on a working day by the 11th of the next month (``gst.late_share`` file a
  few days late, ``FILED_LATE``): every OUTPUT tax row of the period, or of an earlier one, not yet in a return
  (a correction recorded late is reported in the next return), summarised per GSTR-1 section and rate (B2B,
  CDNR, CDNUR; B2CL and B2CS also per place of supply) plus the HSN summary; ``tax_transactions.gst_return_id``
  is set on the included rows (the only update a tax row ever gets).
* **GSTR-3B** (summary and payment), filed by the 20th: output tax of the same rows, the eligible input tax of the
  INPUT rows not yet claimed (``4A5``; ineligible credit is reported in ``4B``), and the **set-off** in the legal
  order (D58, Rule 88A): IGST credit pays IGST, then CGST, then SGST; CGST credit pays CGST, then IGST; SGST
  credit pays SGST, then IGST; CGST and SGST credit never cross; cash pays the rest. Credit not used carries
  forward (it stays on the Input GST accounts): the credit available is all credit claimed in this and earlier
  3Bs less what earlier 3Bs used, so it is read from the returns themselves (D66). Section ``6.1`` lines record
  which credit or cash paid which head. Filing posts **P22** (Dr Output GST of each head by its liability, Cr Input
  GST by the credit used of each type, Cr Bank by the cash) and the cash goes out of the PAYMENTS account the same
  day (the bank step writes the GST_PAYMENT line).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext

HEADS = ("cgst", "sgst", "igst")
# Credit type → the heads it may pay, in the legal order (D58).
SET_OFF = {"igst": ("igst", "cgst", "sgst"), "cgst": ("cgst", "igst"), "sgst": ("sgst", "igst")}


def _previous_period(on: date) -> tuple[str, date, date]:
    end = on.replace(day=1) - timedelta(days=1)
    return f"{end:%Y-%m}", end.replace(day=1), end


def _filing_day(day: DayContext, gstin_id: int, kind: str, period: str, due: date) -> date:
    """The day this return is filed: a working day up to its due date, or a few days late (stream per return)."""
    settings = day.world.config.gst
    rng = day.streams.stream(due, f"gst.filing.{kind.lower()}.{gstin_id}.{period.replace('-', '_')}")
    late = rng.random() < settings.late_share
    target = due + timedelta(days=rng.randint(1, settings.late_days)) if late else \
        due - timedelta(days=rng.randint(0, settings.early_days))
    while not day.world.config.daily.is_working_day(target):
        target += timedelta(days=1) if late else -timedelta(days=1)
    return target


def _registrations(day: DayContext) -> list[dict[str, Any]]:
    if "gst_registrations" not in day.world.refs:
        day.world.refs["gst_registrations"] = day.rows("""
            SELECT r.company_gst_registration_id, r.gstin, s.state_code FROM company_gst_registrations r
            JOIN states s ON s.state_id = r.state_id ORDER BY r.company_gst_registration_id""")
    return day.world.refs["gst_registrations"]


def _arn(gstin: str, kind: str, period: str, filed: date) -> str:
    return f"X{kind[-2:]}{gstin[:2]}{period.replace('-', '')[2:]}{filed:%d%m}{gstin[-4:]}"[:20]


def _new_return(day: DayContext, registration: dict[str, Any], kind: str, period: str, due: date) -> int:
    return_id = day.ids.next("gst_returns")
    at = day.at(day.world.config.gst.filing_hour)
    day.writer.insert("gst_returns", {
        "gst_return_id": return_id, "company_gst_registration_id": registration["company_gst_registration_id"],
        "return_type": kind, "return_period": period, "due_date": due, "filed_date": day.day,
        "arn": _arn(registration["gstin"], kind, period, day.day),
        "output_cgst": ZERO, "output_sgst": ZERO, "output_igst": ZERO, "itc_cgst": ZERO, "itc_sgst": ZERO,
        "itc_igst": ZERO, "itc_used_cgst": ZERO, "itc_used_sgst": ZERO, "itc_used_igst": ZERO,
        "cash_paid_cgst": ZERO, "cash_paid_sgst": ZERO, "cash_paid_igst": ZERO, "payment_date": None,
        "bank_account_id": None, "challan_reference": None,
        "return_status": "FILED_LATE" if day.day > due else "FILED", "created_at": at, "updated_at": at,
    })
    return return_id


def _lines(day: DayContext, return_id: int, rows: list[dict[str, Any]]) -> None:
    at = day.at(day.world.config.gst.filing_hour)
    for number, row in enumerate(rows, 1):
        day.writer.insert("gst_return_line_items", {
            "gst_return_line_item_id": day.ids.next("gst_return_line_items"), "gst_return_id": return_id,
            "line_number": number, "section_code": row["section"], "gst_rate": row.get("gst_rate"),
            "hsn_code": row.get("hsn_code"), "place_of_supply_state_id": row.get("state_id"),
            "paid_through": row.get("paid_through"), "document_count": row.get("documents", 0),
            "taxable_value": row.get("taxable", ZERO), "cgst_amount": row.get("cgst", ZERO),
            "sgst_amount": row.get("sgst", ZERO), "igst_amount": row.get("igst", ZERO),
            "created_at": at, "updated_at": at,
        })


def _gstr1(day: DayContext, registration: dict[str, Any], period: str, due: date) -> None:
    gstin_id = registration["company_gst_registration_id"]
    sections = day.rows("""
        SELECT gstr1_section AS section, gst_rate,
               IF(gstr1_section IN ('B2CL', 'B2CS'), place_of_supply_state_id, NULL) AS state_id,
               COUNT(DISTINCT document_number) AS documents, SUM(taxable_value) AS taxable,
               SUM(cgst_amount) AS cgst, SUM(sgst_amount) AS sgst, SUM(igst_amount) AS igst
        FROM tax_transactions WHERE company_gst_registration_id = :g AND tax_direction = 'OUTPUT'
          AND gst_return_id IS NULL AND return_period <= :p
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", g=gstin_id, p=period)
    if not sections:
        return
    hsn = day.rows("""
        SELECT 'HSN' AS section, hsn_code, gst_rate, COUNT(DISTINCT document_number) AS documents,
               SUM(taxable_value) AS taxable, SUM(cgst_amount) AS cgst, SUM(sgst_amount) AS sgst,
               SUM(igst_amount) AS igst
        FROM tax_transactions WHERE company_gst_registration_id = :g AND tax_direction = 'OUTPUT'
          AND gst_return_id IS NULL AND return_period <= :p
        GROUP BY hsn_code, gst_rate ORDER BY hsn_code, gst_rate""", g=gstin_id, p=period)
    return_id = _new_return(day, registration, "GSTR1", period, due)
    _lines(day, return_id, sections + hsn)
    totals = {h: sum((r[h] for r in sections), ZERO) for h in HEADS}
    day.writer.update("gst_returns", "gst_return_id", return_id,
                      {f"output_{h}": max(totals[h], ZERO) for h in HEADS})
    day.execute("""UPDATE tax_transactions SET gst_return_id = :r, updated_at = GREATEST(updated_at, :t)
                   WHERE company_gst_registration_id = :g AND tax_direction = 'OUTPUT' AND gst_return_id IS NULL
                     AND return_period <= :p""",
                {"r": return_id, "t": day.at(day.world.config.gst.filing_hour), "g": gstin_id, "p": period})


def set_off(liability: dict[str, Decimal], credit: dict[str, Decimal]) -> tuple[list[tuple[str, str, Decimal]], dict[str, Decimal]]:
    """The legal set-off (D58): [(credit type or 'cash', head paid, amount)] and the cash per head."""
    left = dict(liability)
    available = dict(credit)
    used: list[tuple[str, str, Decimal]] = []
    for kind in ("igst", "cgst", "sgst"):
        for head in SET_OFF[kind]:
            amount = min(available[kind], left[head])
            if amount > 0:
                used.append((kind, head, amount))
                available[kind] -= amount
                left[head] -= amount
    return used, left


def _gstr3b(day: DayContext, registration: dict[str, Any], period: str, due: date) -> None:
    gstin_id = registration["company_gst_registration_id"]
    output = day.rows("""
        SELECT COALESCE(SUM(taxable_value), 0) AS taxable, COALESCE(SUM(cgst_amount), 0) AS cgst,
               COALESCE(SUM(sgst_amount), 0) AS sgst, COALESCE(SUM(igst_amount), 0) AS igst,
               COUNT(DISTINCT document_number) AS documents
        FROM tax_transactions t JOIN gst_returns r ON r.gst_return_id = t.gst_return_id
        WHERE t.company_gst_registration_id = :g AND r.return_type = 'GSTR1' AND r.return_period = :p""",
        g=gstin_id, p=period)[0]
    inputs = day.rows("""
        SELECT itc_eligibility, COUNT(DISTINCT document_number) AS documents, COALESCE(SUM(taxable_value), 0) AS taxable,
               COALESCE(SUM(cgst_amount), 0) AS cgst, COALESCE(SUM(sgst_amount), 0) AS sgst,
               COALESCE(SUM(igst_amount), 0) AS igst
        FROM tax_transactions WHERE company_gst_registration_id = :g AND tax_direction = 'INPUT'
          AND gst_return_id IS NULL AND return_period <= :p GROUP BY itc_eligibility ORDER BY 1""", g=gstin_id, p=period)
    eligible = next((r for r in inputs if r["itc_eligibility"] == "ELIGIBLE"), None)
    ineligible = next((r for r in inputs if r["itc_eligibility"] == "INELIGIBLE"), None)
    claimed = {h: eligible[h] if eligible else ZERO for h in HEADS}
    earlier = day.rows("""
        SELECT COALESCE(SUM(itc_cgst - itc_used_cgst), 0) AS cgst, COALESCE(SUM(itc_sgst - itc_used_sgst), 0) AS sgst,
               COALESCE(SUM(itc_igst - itc_used_igst), 0) AS igst
        FROM gst_returns WHERE company_gst_registration_id = :g AND return_type = 'GSTR3B'""", g=gstin_id)[0]
    credit = {h: earlier[h] + claimed[h] for h in HEADS}
    liability = {h: max(output[h], ZERO) for h in HEADS}
    used, cash = set_off(liability, credit)
    return_id = _new_return(day, registration, "GSTR3B", period, due)
    rows = [{"section": "3.1A", "documents": output["documents"], "taxable": output["taxable"],
             **{h: output[h] for h in HEADS}}]
    if eligible:
        rows.append({"section": "4A5", "documents": eligible["documents"], "taxable": eligible["taxable"],
                     **{h: eligible[h] for h in HEADS}})
    if ineligible:
        rows.append({"section": "4B", "documents": ineligible["documents"], "taxable": ineligible["taxable"],
                     **{h: ineligible[h] for h in HEADS}})
    for kind, head, amount in used:
        rows.append({"section": "6.1", "paid_through": f"ITC_{kind.upper()}", head: amount})
    for head in HEADS:
        if cash[head] > 0:
            rows.append({"section": "6.1", "paid_through": "CASH", head: cash[head]})
    _lines(day, return_id, rows)
    used_by_type = {k: sum((a for t, _, a in used if t == k), ZERO) for k in HEADS}
    total_cash = sum(cash.values(), ZERO)
    payments_account = day.rows("SELECT MIN(bank_account_id) AS b FROM bank_accounts WHERE account_purpose = 'PAYMENTS' "
                                "AND account_status = 'ACTIVE'")[0]["b"]
    day.writer.update("gst_returns", "gst_return_id", return_id, {
        **{f"output_{h}": liability[h] for h in HEADS}, **{f"itc_{h}": claimed[h] for h in HEADS},
        **{f"itc_used_{h}": used_by_type[h] for h in HEADS}, **{f"cash_paid_{h}": cash[h] for h in HEADS},
        "payment_date": day.day if total_cash > 0 else None,
        "bank_account_id": payments_account if total_cash > 0 else None,
        "challan_reference": f"XCPIN{registration['gstin'][:2]}{period.replace('-', '')}{gstin_id:03d}"[:20]
        if total_cash > 0 else None})
    day.execute("""UPDATE tax_transactions SET gst_return_id = :r, updated_at = GREATEST(updated_at, :t)
                   WHERE company_gst_registration_id = :g AND tax_direction = 'INPUT' AND gst_return_id IS NULL
                     AND return_period <= :p""",
                {"r": return_id, "t": day.at(day.world.config.gst.filing_hour), "g": gstin_id, "p": period})
    book = account_book(day)
    if sum(liability.values(), ZERO) == 0:
        return
    builder = JournalBuilder(f"GSTR-3B {registration['gstin']} {period}")
    for head in HEADS:
        builder.debit(book.tax(gstin_id, head.upper(), "OUTPUT"), liability[head])
    for kind in HEADS:
        builder.credit(book.tax(gstin_id, kind.upper(), "INPUT"), used_by_type[kind])
    if total_cash > 0:
        builder.credit(book.bank(payments_account), total_cash)
    post_journal(day, builder.build(), source="GST_SETTLEMENT", gst_return_id=return_id)


def gst_step(day: DayContext) -> None:
    """Day step (before the bank step, which pays the 3B cash): this month's returns of last month, per GSTIN."""
    settings = day.world.config.gst
    period, start, end = _previous_period(day.day)
    if start < day.world.config.start_date.replace(day=1):
        return
    gstr1_due = day.day.replace(day=settings.gstr1_due_day)
    gstr3b_due = day.day.replace(day=settings.gstr3b_due_day)
    for registration in _registrations(day):
        gstin_id = registration["company_gst_registration_id"]
        if _filing_day(day, gstin_id, "GSTR1", period, gstr1_due) == day.day:
            _gstr1(day, registration, period, gstr1_due)
        if _filing_day(day, gstin_id, "GSTR3B", period, gstr3b_due) == day.day:
            _gstr3b(day, registration, period, gstr3b_due)
