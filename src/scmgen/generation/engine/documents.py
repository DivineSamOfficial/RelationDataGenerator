"""Helpers shared by the document processes (procurement, sales, finance).

* ``record_status`` writes one ``<doc>_status_history`` row per transition (1C §2.2: append-only, the first
  entry has no from-status).
* ``rate_on`` is the product's GST rate on a date (D17, ``product_tax_rates``), cached for the day.
* ``price_lines`` / ``totals`` turn quantities and prices into the 1C §2.3 amount columns (D84).
* ``approver`` picks the lowest-level active employee whose approval limit covers an amount.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from scmgen.domain.gst import document_totals, price_exclusive, split_gst, tax_line
from scmgen.generation.engine.day import DayContext


def record_status(day: DayContext, document: str, document_id: int, from_status: str | None, to_status: str,
                  at: datetime, by: int | None = None, reason: str | None = None) -> None:
    day.writer.insert(f"{document}_status_history", {
        f"{document}_status_history_id": day.ids.next(f"{document}_status_history"), f"{document}_id": document_id,
        "from_status": from_status, "to_status": to_status, "changed_at": at, "changed_by_employee_id": by,
        "change_reason": reason, "created_at": at, "updated_at": at,
    })


def preload_rates(day: DayContext, product_ids: Iterable[int], on: date) -> None:
    """Load the GST rates on ``on`` of the given products in one query (only those not cached yet today)."""
    cache = day.notes.setdefault("rates", {}).setdefault(on, {})
    missing = sorted({p for p in product_ids} - cache.keys())
    if missing:
        for row in day.rows(f"""
                SELECT product_id, gst_rate FROM product_tax_rates WHERE effective_from <= :d
                  AND (effective_to IS NULL OR effective_to >= :d) AND product_id IN ({", ".join(map(str, missing))})""",
                d=on):
            cache[row["product_id"]] = Decimal(row["gst_rate"])


def rate_on(day: DayContext, product_id: int, on: date) -> Decimal:
    """The product's GST rate on ``on`` (cached for the day; ``preload_rates`` fetches many at once)."""
    preload_rates(day, (product_id,), on)
    rate = day.notes["rates"][on].get(product_id)
    if rate is None:
        raise RuntimeError(f"Product {product_id} has no GST rate on {on}")
    return rate


def price_lines(items: Iterable[dict[str, Any]], inter_state: bool, quantity_key: str) -> list[dict[str, Any]]:
    """Amount columns for document lines priced excluding GST. Each item needs ``quantity_key``, ``unit_price``,
    ``discount_percent`` and ``gst_rate``; returns the items with taxable / CGST / SGST / IGST / line total."""
    priced = []
    for item in items:
        line = price_exclusive(item[quantity_key], item["unit_price"], item["discount_percent"], item["gst_rate"])
        split = split_gst(line.taxable_amount, line.gst_rate, inter_state)
        priced.append({**item, quantity_key: line.quantity, "unit_price": line.unit_price,
                       "discount_percent": line.discount_percent, "gst_rate": line.gst_rate,
                       "taxable_amount": line.taxable_amount, "cgst_amount": split.cgst, "sgst_amount": split.sgst,
                       "igst_amount": split.igst, "line_total": line.taxable_amount + split.total})
    return priced


def totals(lines: list[dict[str, Any]], inter_state: bool) -> dict[str, Decimal]:
    """Header totals (Σ lines + round-off to the rupee) for priced lines."""
    result = document_totals(tax_line(l["taxable_amount"], l["gst_rate"], inter_state) for l in lines)
    return {"taxable_amount": result.taxable_amount, "cgst_amount": result.cgst_amount,
            "sgst_amount": result.sgst_amount, "igst_amount": result.igst_amount,
            "round_off_amount": result.round_off_amount, "total_amount": result.total_amount}


def _approvers(day: DayContext, on: date) -> list[dict[str, Any]]:
    key = ("approvers", on)
    if key not in day.notes:
        day.notes[key] = day.rows("""
            SELECT e.employee_id, e.approval_limit, e.job_level, d.department_code FROM employees e
            JOIN departments d USING (department_id)
            WHERE e.employment_status = 'ACTIVE' AND e.hire_date <= :d AND e.approval_limit > 0
            ORDER BY e.approval_limit, e.employee_id""", d=on)
    return day.notes[key]


def max_approval(day: DayContext, departments: tuple[str, ...], on: date) -> Decimal:
    """The largest amount anyone in the departments (or the CEO) can approve on ``on``."""
    return max(r["approval_limit"] for r in _approvers(day, on)
               if r["department_code"] in departments or r["job_level"] == "EXECUTIVE")


def approver(day: DayContext, amount: Decimal, departments: tuple[str, ...], on: date) -> int:
    """The active employee with the smallest sufficient approval limit in the given departments (or the CEO)."""
    for row in _approvers(day, on):
        if row["approval_limit"] >= amount and (row["department_code"] in departments or row["job_level"] == "EXECUTIVE"):
            return row["employee_id"]
    raise RuntimeError(f"Nobody can approve {amount} on {on}")


def staff(day: DayContext, department: str, levels: tuple[str, ...], on: date) -> list[int]:
    """Active employees of a department at the given levels, in id order (deterministic choices)."""
    key = ("staff", department, levels, on)
    if key not in day.notes:
        day.notes[key] = [r["employee_id"] for r in day.rows(f"""
            SELECT e.employee_id FROM employees e JOIN departments d USING (department_id)
            WHERE d.department_code = :dep AND e.employment_status = 'ACTIVE' AND e.hire_date <= :d
              AND e.warehouse_id IS NULL AND e.job_level IN ({", ".join(f"'{l}'" for l in levels)})
            ORDER BY e.employee_id""", dep=department, d=on)]
    return day.notes[key]
