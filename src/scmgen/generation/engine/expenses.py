"""Expenses and payroll (1E §2, §6 P19/P20; D56, D57; step 3.13).

Every non-payroll expense goes through AP (D57): a document is **received** by Accounts, **approved** by Finance a
few days later (P19: Dr the category's expense account per line on its cost centre, Dr Input GST of the bill-to
GSTIN for the credit-eligible part, Dr *GST – Ineligible Input Credit* for the rest, Cr AP of the payee; INPUT tax
rows for registered suppliers) and **paid** in the weekly expense payment run (P20: Dr AP / Cr Bank). About
``rejection_share`` of ad-hoc bills and claims are rejected and post nothing.

* **Site costs** (per warehouse, monthly): rent billed on the 1st for the month by the site's landlord; electricity
  (seasonal), water, security, housekeeping, internet and contract labour billed for the previous month. Each
  site's base amount is drawn once (stream keyed by site × category) and escalates every April.
* **Company costs** (monthly): SaaS, AI tools, cloud hosting (base + per order of the month), packaging (per
  shipment), per supplier. **Annual**: insurance and audit fees in their month.
* **Usage bills** (monthly, for the previous month): each carrier's freight bill = Σ its shipments' freight per
  warehouse (V-SH-05); the marketplace commission = Σ marketplace invoices' taxable value × the pack's commission
  per business unit; each completed campaign's ad spend ≈ its budget, billed after it ends (A1).
* **Ad hoc** (Poisson per month, drawn per day): repairs, legal, recruitment, stationery, office supplies, staff
  welfare, print and events, courier; **travel claims** by employees; **petty cash** vouchers per warehouse from a
  local shop (unregistered), approved and paid the same day in cash from the warehouse's float (≤ ₹10,000).
* **Payroll** (D56): on the last day of the month one PAYROLL expense, a line per cost centre = Σ salaries of the
  employees active in the month, pro-rated for joiners and leavers; P19 Dr Salaries / Cr Salaries Payable (no AP
  item); paid on the first working day of the next month from the PAYROLL account (PAYMENTS when there is none),
  P20 Dr Salaries Payable / Cr Bank.

A recurring bill's dates come from a stream keyed by its recurrence key and service period (D65), so whether it
exists is a property of the documents (D66). Supporting-document metadata (fictional ``.example`` storage, D24)
is attached to ``attachment_share`` of the non-payroll expenses.
"""

from __future__ import annotations

import calendar
import hashlib
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import ZERO, money, product, sql_divide, total, whole_rupees
from scmgen.domain.gst import document_totals, split_gst, tax_line
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext, poisson
from scmgen.generation.engine.documents import approver, max_approval
from scmgen.generation.numbering import Registration

REJECTIONS = ("Duplicate of an earlier bill", "GSTIN missing on the invoice", "Not within the approved budget",
              "Supporting documents missing")
PAYMENT_FAILURES = ("Beneficiary account details mismatch", "Beneficiary bank unavailable")
MARKETING = "MARKETING"
PETTY_LIMIT = Decimal(10000)


@dataclass
class _Line:
    category: str
    cost_center_id: int
    description: str
    taxable: Decimal
    campaign_id: int | None = None


@dataclass
class _Draft:
    expense_type: str
    expense_date: date
    received_date: date
    description: str
    lines: list[_Line]
    supplier_id: int | None = None
    employee_id: int | None = None
    warehouse_id: int | None = None
    vendor_bill_number: str | None = None
    recurrence_key: str | None = None
    period: tuple[date, date] | None = None
    rejectable: bool = False
    attachments: tuple[str, ...] = ("VENDOR_INVOICE",)


# ------------------------------------------------------------------------------------------ reference data


class _Reference:
    """Categories, suppliers by category, sites, cost centres and banks (cached per run)."""

    def __init__(self, day: DayContext) -> None:
        self.categories = {r["category_code"]: r for r in day.rows("""
            SELECT c.expense_category_id, c.category_code, c.category_group, c.default_gst_rate, c.is_itc_eligible,
                   c.gl_account_id AS account_id FROM expense_categories c WHERE c.is_active""")}
        by_id = {c["expense_category_id"]: code for code, c in self.categories.items()}
        self.suppliers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in day.rows("""
                SELECT s.supplier_id, s.supplier_name, s.gst_registration_type, s.gstin, s.default_expense_category_id,
                       t.due_days, a.city, a.state_id
                FROM suppliers s JOIN payment_terms t ON t.payment_term_id = s.payment_term_id
                LEFT JOIN supplier_addresses a ON a.supplier_id = s.supplier_id AND a.address_type = 'REGISTERED'
                     AND NOT EXISTS (SELECT 1 FROM supplier_addresses n WHERE n.previous_supplier_address_id = a.supplier_address_id)
                WHERE s.supplier_status = 'ACTIVE' AND s.default_expense_category_id IS NOT NULL
                ORDER BY s.supplier_id"""):
            self.suppliers[by_id[row["default_expense_category_id"]]].append(row)
        self.carriers = {r["shipping_carrier_id"]: r["supplier_id"] for r in day.rows(
            "SELECT shipping_carrier_id, supplier_id FROM shipping_carriers WHERE supplier_id IS NOT NULL")}
        self.carrier_suppliers = {r["supplier_id"]: r for r in day.rows(f"""
            SELECT s.supplier_id, s.supplier_name, s.gst_registration_type, s.gstin, t.due_days, a.city, a.state_id
            FROM suppliers s JOIN payment_terms t ON t.payment_term_id = s.payment_term_id
            LEFT JOIN supplier_addresses a ON a.supplier_id = s.supplier_id AND a.address_type = 'REGISTERED'
                     AND NOT EXISTS (SELECT 1 FROM supplier_addresses n WHERE n.previous_supplier_address_id = a.supplier_address_id)
            WHERE s.supplier_id IN ({", ".join(map(str, sorted(self.carriers.values()))) or "0"})""")}
        self.sites = {r["warehouse_id"]: r for r in day.rows("""
            SELECT w.warehouse_id, w.warehouse_code, w.warehouse_type, w.city, w.company_gst_registration_id,
                   g.state_id, s.state_code, c.cost_center_id
            FROM warehouses w JOIN company_gst_registrations g ON g.company_gst_registration_id = w.company_gst_registration_id
            JOIN states s ON s.state_id = g.state_id JOIN cost_centers c ON c.warehouse_id = w.warehouse_id
            WHERE w.warehouse_status = 'ACTIVE' ORDER BY w.warehouse_id""")}
        principal = day.rows("""SELECT r.company_gst_registration_id, r.state_id, s.state_code FROM company_gst_registrations r
                                JOIN states s ON s.state_id = r.state_id WHERE r.is_principal_place""")[0]
        self.principal = principal
        self.departments = {r["department_code"]: r["cost_center_id"] for r in day.rows(
            "SELECT d.department_code, c.cost_center_id FROM cost_centers c JOIN departments d USING (department_id)")}
        self.units = {r["business_unit_id"]: r for r in day.rows(
            "SELECT b.business_unit_id, b.industry_pack_code, c.cost_center_id FROM business_units b "
            "JOIN cost_centers c ON c.business_unit_id = b.business_unit_id")}
        banks = day.rows("SELECT bank_account_id, account_purpose, warehouse_id FROM bank_accounts "
                         "WHERE account_status = 'ACTIVE' ORDER BY bank_account_id")
        self.payments_bank = next(b["bank_account_id"] for b in banks if b["account_purpose"] == "PAYMENTS")
        self.payroll_bank = next((b["bank_account_id"] for b in banks if b["account_purpose"] == "PAYROLL"),
                                 self.payments_bank)
        self.petty = {b["warehouse_id"]: b["bank_account_id"] for b in banks if b["account_purpose"] == "PETTY_CASH"}
        self.methods = {r["method_code"]: r for r in day.rows(
            "SELECT payment_method_id, method_code, min_amount FROM payment_methods")}


def _reference(day: DayContext) -> _Reference:
    if "expense_reference" not in day.world.refs:
        day.world.refs["expense_reference"] = _Reference(day)
    return day.world.refs["expense_reference"]


def _cost_center(ref: _Reference, category: str, warehouse_id: int | None, settings) -> int:
    rule = settings.cost_centres[category]
    if rule == "WAREHOUSE":
        if warehouse_id is None:
            raise RuntimeError(f"{category} needs a warehouse")
        return ref.sites[warehouse_id]["cost_center_id"]
    return ref.departments[rule]


def _month_start(on: date) -> date:
    return on.replace(day=1)


def _previous_month(on: date) -> tuple[date, date]:
    end = on.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def _working_on_or_after(day: DayContext, on: date) -> date:
    while not day.world.config.daily.is_working_day(on):
        on += timedelta(days=1)
    return on


# ------------------------------------------------------------------------------------------ recurring bills


def _escalated(day: DayContext, base: Decimal, period_start: date) -> Decimal:
    """Base amount escalated every April since the history start."""
    settings = day.world.config.expenses
    years = max(0, (period_start.year - day.world.config.start_date.year)
                + (0 if period_start.month >= 4 else -1) - (0 if day.world.config.start_date.month >= 4 else -1))
    return money(base * (1 + Decimal(str(settings.annual_escalation))) ** years)


def _stream_key(key: str) -> str:
    """A recurrence key (``RENT-DC-BNG``) as a stream-name word (``rent_dc_bng``)."""
    return key.lower().replace("-", "_")


def scale(day: DayContext) -> Decimal:
    """Company size relative to the volume the amounts are set for (Medium): premises, contracts and bill counts
    follow it (payroll follows the headcount on its own)."""
    settings = day.world.config.expenses
    return Decimal(day.world.config.volumes.orders_per_day) / settings.reference_orders_per_day


def _base(day: DayContext, key: str, low: int, high: int) -> Decimal:
    rng = day.streams.stream(day.world.config.start_date, f"expenses.base.{_stream_key(key)}")
    return money(Decimal(rng.randint(low, high)) * scale(day))


def _bill_dates(day: DayContext, key: str, period_start: date, in_advance: bool) -> tuple[date, date]:
    """(bill date, date it reaches Accounts) for a recurring bill: rent on the 1st of its month; the others for
    the previous month within the first days of the next."""
    rng = day.streams.stream(period_start, f"expenses.bill.{_stream_key(key)}")
    if in_advance:
        billed = period_start
    else:
        next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        billed = next_month + timedelta(days=rng.randint(*day.world.config.expenses.bill_day_range) - 1)
    return billed, _working_on_or_after(day, billed + timedelta(days=rng.randint(0, 3)))


def _site_bills(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    settings = day.world.config.expenses
    start = day.world.config.start_date
    for category, by_type in sorted(settings.site_costs.items()):
        in_advance = category in settings.billed_in_advance
        period_start = _month_start(day.day) if in_advance else _previous_month(day.day)[0]
        for candidate in (period_start, _previous_month(period_start)[0] if not in_advance else None):
            if candidate is None or candidate < _month_start(start):
                continue
            for warehouse_id, site in sorted(ref.sites.items()):
                band = by_type.get(site["warehouse_type"])
                suppliers = ref.suppliers.get(category, [])
                if not band or not suppliers:
                    continue
                key = f"{category.replace('_', '-')}-{site['warehouse_code']}"
                billed, received = _bill_dates(day, key, candidate, in_advance)
                if received != day.day:
                    continue
                supplier = _site_supplier(category, suppliers, site, warehouse_id, ref)
                amount = _escalated(day, _base(day, key, *band), candidate)
                if category == "ELECTRICITY":
                    amount = money(amount * Decimal(str(settings.electricity_season[candidate.month - 1])))
                end = (candidate.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                drafts.append(_Draft(
                    "VENDOR_BILL", billed, received, f"{category.replace('_', ' ').title()} {site['warehouse_code']} "
                    f"{candidate:%b %Y}", [_Line(category, site["cost_center_id"],
                                                  f"{category.replace('_', ' ').title()} {candidate:%b %Y}", amount)],
                    supplier_id=supplier["supplier_id"], warehouse_id=warehouse_id,
                    vendor_bill_number=_bill_number(supplier, key, candidate), recurrence_key=key,
                    period=(candidate, end)))


def _site_supplier(category: str, suppliers: list[dict[str, Any]], site: dict[str, Any], warehouse_id: int,
                   ref: _Reference) -> dict[str, Any]:
    """The site's landlord / the city's utility / a service supplier (fixed per site)."""
    local = [s for s in suppliers if s["city"] == site["city"]]
    pool = local or suppliers
    index = sorted(ref.sites).index(warehouse_id)
    return pool[index % len(pool)]


def _bill_number(supplier: dict[str, Any], key: str, period: date) -> str:
    digest = hashlib.sha256(f"{supplier['supplier_id']}|{key}|{period}".encode()).hexdigest()[:6].upper()
    return f"B{period:%y%m}-{digest}"


def _company_bills(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    settings = day.world.config.expenses
    start = _month_start(day.world.config.start_date)
    for category, band in sorted(settings.company_costs.items()):
        for index, supplier in enumerate(ref.suppliers.get(category, [])):
            key = f"{category.replace('_', '-')}-{supplier['supplier_id']}"
            period_start, period_end = _previous_month(day.day)
            for candidate in (period_start, _previous_month(period_start)[0]):
                if candidate < start:
                    continue
                billed, received = _bill_dates(day, key, candidate, False)
                if received != day.day:
                    continue
                end = (candidate.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                amount = _escalated(day, _base(day, key, *band), candidate)
                amount += _usage(day, category, candidate, end, len(ref.suppliers[category]))
                drafts.append(_Draft(
                    "VENDOR_BILL", billed, received, f"{category.replace('_', ' ').title()} {candidate:%b %Y}",
                    [_Line(category, _cost_center(ref, category, None, settings),
                           f"{supplier['supplier_name']} {candidate:%b %Y}", money(amount))],
                    supplier_id=supplier["supplier_id"], vendor_bill_number=_bill_number(supplier, key, candidate),
                    recurrence_key=key, period=(candidate, end)))


def _usage(day: DayContext, category: str, start: date, end: date, suppliers: int) -> Decimal:
    """Volume-driven part of a company bill (cloud per order, packaging per shipment), split across suppliers."""
    settings = day.world.config.expenses
    if category == "CLOUD" and settings.cloud_per_order:
        orders = day.rows("SELECT COUNT(*) AS n FROM orders WHERE order_date BETWEEN :a AND :b", a=start, b=end)[0]["n"]
        return money(Decimal(str(settings.cloud_per_order)) * orders / suppliers)
    if category == "PACKAGING" and settings.packaging_per_shipment:
        shipments = day.rows("SELECT COUNT(*) AS n FROM shipments WHERE dispatched_at >= :a AND dispatched_at < :b",
                             a=start, b=end + timedelta(days=1))[0]["n"]
        return money(Decimal(str(settings.packaging_per_shipment)) * shipments / suppliers)
    return ZERO


def _annual_bills(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    settings = day.world.config.expenses
    for category, (month, low, high) in sorted(settings.annual_costs.items()):
        if day.day.month != month or not ref.suppliers.get(category):
            continue
        supplier = ref.suppliers[category][0]
        key = f"{category.replace('_', '-')}-{day.day.year}"
        period = date(day.day.year, month, 1)
        if period < _month_start(day.world.config.start_date):
            continue
        billed, received = _bill_dates(day, key, period, True)
        if received != day.day:
            continue
        drafts.append(_Draft(
            "VENDOR_BILL", billed, received, f"{category.replace('_', ' ').title()} {day.day.year}",
            [_Line(category, _cost_center(ref, category, None, settings), f"{category.replace('_', ' ').title()}",
                   money(_base(day, key, low, high)))],
            supplier_id=supplier["supplier_id"], vendor_bill_number=_bill_number(supplier, key, period),
            recurrence_key=key, period=(period, date(period.year + 1, month, 1) - timedelta(days=1)),
            attachments=("VENDOR_INVOICE", "CONTRACT")))


# ------------------------------------------------------------------------------------------ usage bills


def _freight_bills(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    """Each carrier bills last month's freight, one line per dispatching warehouse (V-SH-05)."""
    start, end = _previous_month(day.day)
    if start < _month_start(day.world.config.start_date):
        return
    for carrier_id, supplier_id in sorted(ref.carriers.items()):
        key = f"FREIGHT-{carrier_id}"
        billed, received = _bill_dates(day, key, start, False)
        if received != day.day:
            continue
        rows = day.rows("""
            SELECT warehouse_id, SUM(freight_cost_amount) AS freight FROM shipments
            WHERE shipping_carrier_id = :c AND dispatched_at >= :a AND dispatched_at < :b AND freight_cost_amount > 0
            GROUP BY warehouse_id ORDER BY warehouse_id""", c=carrier_id, a=start, b=end + timedelta(days=1))
        if not rows:
            continue
        supplier = ref.carrier_suppliers[supplier_id]
        drafts.append(_Draft(
            "VENDOR_BILL", billed, received, f"Freight {start:%b %Y}",
            [_Line("FREIGHT_OUTWARD", ref.sites[r["warehouse_id"]]["cost_center_id"],
                   f"Freight {ref.sites[r['warehouse_id']]['warehouse_code']} {start:%b %Y}", r["freight"]) for r in rows],
            supplier_id=supplier_id, vendor_bill_number=_bill_number(supplier, key, start), recurrence_key=key,
            period=(start, end)))


def _marketplace_bill(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    """The marketplace bills its commission on last month's marketplace sales, per business unit."""
    start, end = _previous_month(day.day)
    suppliers = ref.suppliers.get("MARKETPLACE_FEES", [])
    if start < _month_start(day.world.config.start_date) or not suppliers:
        return
    supplier = suppliers[0]
    key = "MARKETPLACE-FEES"
    billed, received = _bill_dates(day, key, start, False)
    if received != day.day:
        return
    packs = day.world.packs_by_code
    lines = []
    for row in day.rows("""
            SELECT p.business_unit_id, SUM(il.taxable_amount) AS sales FROM invoices i
            JOIN orders o ON o.order_id = i.order_id JOIN sales_channels c ON c.sales_channel_id = o.sales_channel_id
            JOIN invoice_line_items il ON il.invoice_id = i.invoice_id JOIN products p ON p.product_id = il.product_id
            WHERE c.channel_code = 'MARKETPLACE' AND i.invoice_status = 'ISSUED' AND i.invoice_date BETWEEN :a AND :b
            GROUP BY p.business_unit_id ORDER BY p.business_unit_id""", a=start, b=end):
        unit = ref.units[row["business_unit_id"]]
        rate = packs[unit["industry_pack_code"]].marketplace_commission_percent
        amount = money(product(row["sales"], rate) / 100)
        if amount > 0:
            lines.append(_Line("MARKETPLACE_FEES", unit["cost_center_id"], f"Commission {start:%b %Y}", amount))
    if lines:
        drafts.append(_Draft("VENDOR_BILL", billed, received, f"Marketplace commission {start:%b %Y}", lines,
                             supplier_id=supplier["supplier_id"], vendor_bill_number=_bill_number(supplier, key, start),
                             recurrence_key=key, period=(start, end)))


def _campaign_bills(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    """A completed campaign's media spend (≈ its budget) is billed within two weeks of its end (A1)."""
    settings = day.world.config.expenses
    suppliers = ref.suppliers.get("DIGITAL_ADS", [])
    if not suppliers:
        return
    for campaign in day.rows("""
            SELECT marketing_campaign_id, campaign_name, end_date, budget_amount FROM marketing_campaigns
            WHERE campaign_status = 'COMPLETED' AND end_date BETWEEN :a AND :b AND end_date >= :s
            ORDER BY marketing_campaign_id""", a=day.day - timedelta(days=20), b=day.day, s=day.world.config.start_date):
        rng = day.streams.stream(campaign["end_date"], f"expenses.campaign.{campaign['marketing_campaign_id']}")
        billed = campaign["end_date"] + timedelta(days=rng.randint(1, 7))
        received = _working_on_or_after(day, billed + timedelta(days=rng.randint(0, 3)))
        if received != day.day:
            continue
        supplier = suppliers[campaign["marketing_campaign_id"] % len(suppliers)]
        spend = money(campaign["budget_amount"] * Decimal(str(rng.uniform(*settings.campaign_spend_share))))
        drafts.append(_Draft(
            "VENDOR_BILL", billed, received, f"Media spend: {campaign['campaign_name']}"[:255],
            [_Line("DIGITAL_ADS", _cost_center(ref, "DIGITAL_ADS", None, settings),
                   f"Media spend {campaign['campaign_name']}"[:255], spend, campaign["marketing_campaign_id"])],
            supplier_id=supplier["supplier_id"],
            vendor_bill_number=_bill_number(supplier, f"CAMPAIGN-{campaign['marketing_campaign_id']}", billed)))


# ------------------------------------------------------------------------------------------ ad hoc


def _ad_hoc(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    settings = day.world.config.expenses
    if not day.is_working_day:
        return
    rng = day.rng("expenses.ad_hoc")
    for category, (per_month, low, high) in sorted(settings.ad_hoc.items()):
        suppliers = ref.suppliers.get(category, [])
        if not suppliers:
            continue
        for _ in range(poisson(rng, per_month * float(scale(day)) / 26)):
            supplier = rng.choice(suppliers)
            warehouse_id = rng.choice(sorted(ref.sites)) if settings.cost_centres[category] == "WAREHOUSE" else None
            billed = day.day - timedelta(days=rng.randint(0, 6))
            amount = money(Decimal(rng.randint(low, high)))
            drafts.append(_Draft(
                "VENDOR_BILL", billed, day.day, f"{category.replace('_', ' ').title()}",
                [_Line(category, _cost_center(ref, category, warehouse_id, settings),
                       f"{category.replace('_', ' ').title()} {billed:%d %b}", amount)],
                supplier_id=supplier["supplier_id"], warehouse_id=warehouse_id,
                vendor_bill_number=f"AH{billed:%y%m%d}-{rng.randint(100000, 999999)}", rejectable=True))
    # Travel claims by employees (sales, purchasing, management travel most).
    claimants = day.rows("""SELECT e.employee_id, e.cost_center_id FROM employees e JOIN departments d USING (department_id)
                            WHERE e.employment_status = 'ACTIVE' AND e.hire_date <= :d
                              AND d.department_code IN ('SAL', 'PUR', 'MGMT', 'LOG', 'MKT') ORDER BY e.employee_id""",
                         d=day.day) if settings.travel_claims_per_month else []
    for _ in range(poisson(rng, settings.travel_claims_per_month * float(scale(day)) / 26) if claimants else 0):
        claimant = rng.choice(claimants)
        travelled = day.day - timedelta(days=rng.randint(2, 20))
        drafts.append(_Draft(
            "EMPLOYEE_CLAIM", travelled, day.day, "Travel claim",
            [_Line("TRAVEL", claimant["cost_center_id"], f"Travel {travelled:%d %b}",
                   money(Decimal(rng.randint(*settings.travel_claim_amount))))],
            employee_id=claimant["employee_id"], rejectable=True, attachments=("RECEIPT", "TRAVEL_TICKET")))


def _petty_cash(day: DayContext, ref: _Reference, drafts: list[_Draft]) -> None:
    settings = day.world.config.expenses
    if not day.is_working_day:
        return
    shops = ref.suppliers.get("PETTY_CASH", [])
    if not shops:
        return
    for warehouse_id, site in sorted(ref.sites.items()):
        if warehouse_id not in ref.petty:
            continue
        rng = day.rng(f"expenses.petty.{warehouse_id}")
        for _ in range(poisson(rng, sum(settings.petty_cash_per_month) / 2 * min(1.0, float(scale(day))) / 26)):
            amount = min(PETTY_LIMIT, money(Decimal(rng.randint(*settings.petty_cash_amount))))
            drafts.append(_Draft(
                "PETTY_CASH", day.day, day.day, f"Petty cash {site['warehouse_code']}",
                [_Line("PETTY_CASH", site["cost_center_id"], rng.choice(settings.petty_cash_items), amount)],
                supplier_id=_site_supplier("PETTY_CASH", shops, site, warehouse_id, ref)["supplier_id"],
                warehouse_id=warehouse_id, attachments=("RECEIPT",)))


# ------------------------------------------------------------------------------------------ recording


def _registration(ref: _Reference, draft: _Draft) -> tuple[int, int, str]:
    """(GSTIN id, its state id, state code) the bill is addressed to: the site's, else the principal place."""
    if draft.warehouse_id is not None:
        site = ref.sites[draft.warehouse_id]
        return site["company_gst_registration_id"], site["state_id"], site["state_code"]
    p = ref.principal
    return p["company_gst_registration_id"], p["state_id"], p["state_code"]


def _supplier_row(ref: _Reference, supplier_id: int) -> dict[str, Any]:
    if supplier_id in ref.carrier_suppliers:
        return ref.carrier_suppliers[supplier_id]
    for rows in ref.suppliers.values():
        for row in rows:
            if row["supplier_id"] == supplier_id:
                return row
    raise KeyError(supplier_id)


def _record(day: DayContext, ref: _Reference, draft: _Draft, rng: random.Random) -> int:
    """Write the expense SUBMITTED (petty cash: approved and paid at once)."""
    settings = day.world.config.expenses
    registration_id, bill_state, state_code = _registration(ref, draft)
    supplier = _supplier_row(ref, draft.supplier_id) if draft.supplier_id else None
    registered = bool(supplier and supplier["gst_registration_type"] == "REGISTERED")
    inter_state = bool(registered and supplier["state_id"] is not None and supplier["state_id"] != bill_state)
    rows = []
    for line in draft.lines:
        category = ref.categories[line.category]
        rate = Decimal(category["default_gst_rate"]) if registered and category["default_gst_rate"] is not None else ZERO
        split = split_gst(line.taxable, rate, inter_state)
        rows.append((line, category, rate, split, bool(category["is_itc_eligible"]) and rate > 0))
    totals = document_totals(tax_line(l.taxable, r, inter_state) for l, _, r, _, _ in rows)
    tax = totals.cgst_amount + totals.sgst_amount + totals.igst_amount
    eligible = total(s.total for _, _, _, s, e in rows if e)
    expense_id = day.ids.next("expenses")
    submitted_at = day.at(rng.randint(10, 13), rng.randint(0, 59))
    submitter = rng.choice(_staff(day, "FIN"))
    number = day.numbers.next(day.conn, "EXP", day.day, Registration(registration_id, state_code))
    due = draft.expense_date + timedelta(days=supplier["due_days"] if supplier else 0)
    if draft.expense_type == "EMPLOYEE_CLAIM":
        due = draft.received_date + timedelta(days=settings.claim_due_days)
    due = max(due, draft.expense_date)
    day.writer.insert("expenses", {
        "expense_id": expense_id, "expense_number": number, "expense_type": draft.expense_type,
        "supplier_id": draft.supplier_id, "employee_id": draft.employee_id, "vendor_bill_number": draft.vendor_bill_number,
        "expense_date": draft.expense_date, "received_date": draft.received_date, "due_date": due,
        "service_period_start": draft.period[0] if draft.period else None,
        "service_period_end": draft.period[1] if draft.period else None,
        "company_gst_registration_id": registration_id if tax > 0 else None, "warehouse_id": draft.warehouse_id,
        "is_inter_state": inter_state, "taxable_amount": totals.taxable_amount, "cgst_amount": totals.cgst_amount,
        "sgst_amount": totals.sgst_amount, "igst_amount": totals.igst_amount, "round_off_amount": totals.round_off_amount,
        "total_amount": totals.total_amount, "itc_eligible_tax_amount": eligible, "recurrence_key": draft.recurrence_key,
        "expense_status": "SUBMITTED", "submitted_by_employee_id": submitter, "approved_by_employee_id": None,
        "approved_at": None, "rejection_reason": None, "description": draft.description[:255],
        "created_at": submitted_at, "updated_at": submitted_at,
    })
    for number_, (line, category, rate, split, is_eligible) in enumerate(rows, 1):
        day.writer.insert("expense_line_items", {
            "expense_line_item_id": day.ids.next("expense_line_items"), "expense_id": expense_id,
            "line_number": number_, "expense_category_id": category["expense_category_id"],
            "cost_center_id": line.cost_center_id, "marketing_campaign_id": line.campaign_id,
            "description": line.description[:255], "sac_code": settings.sac_codes.get(line.category),
            "taxable_amount": line.taxable, "gst_rate": rate, "cgst_amount": split.cgst, "sgst_amount": split.sgst,
            "igst_amount": split.igst, "is_itc_eligible": is_eligible, "line_total": line.taxable + split.total,
            "created_at": submitted_at, "updated_at": submitted_at,
        })
    if draft.expense_type != "PAYROLL" and rng.random() < settings.attachment_share:
        for index, kind in enumerate(draft.attachments[:rng.randint(1, len(draft.attachments))], 1):
            uri = f"s3://docs.example/expenses/{draft.received_date:%Y/%m}/{number.replace('/', '-')}-{index}.pdf"
            uploaded = submitted_at - timedelta(minutes=rng.randint(1, 30))
            day.writer.insert("expense_attachments_metadata", {
                "expense_attachment_id": day.ids.next("expense_attachments_metadata"), "expense_id": expense_id,
                "document_type": kind, "file_name": uri.rsplit("/", 1)[1],
                "mime_type": "application/pdf" if kind != "RECEIPT" else rng.choice(("image/jpeg", "image/png")),
                "file_size_bytes": rng.randint(40_000, 2_500_000), "storage_uri": uri,
                "sha256_checksum": hashlib.sha256(uri.encode()).hexdigest(),
                "uploaded_at": uploaded, "uploaded_by_employee_id": submitter,
                "created_at": uploaded, "updated_at": uploaded,   # insert-only (APPEND in the loader, V-LD-02)
            })
    return expense_id


def _staff(day: DayContext, department: str) -> list[int]:
    key = ("expense_staff", department, day.day)
    if key not in day.notes:
        day.notes[key] = [r["employee_id"] for r in day.rows("""
            SELECT e.employee_id FROM employees e JOIN departments d USING (department_id)
            WHERE d.department_code = :dep AND e.employment_status = 'ACTIVE' AND e.hire_date <= :d
            ORDER BY e.employee_id""", dep=department, d=day.day)]
    return day.notes[key]


# ------------------------------------------------------------------------------------------ approval and posting


def approvals(day: DayContext) -> None:
    """Finance approves (or rejects) submitted expenses 1–5 working days after receipt; petty cash at once."""
    settings = day.world.config.expenses
    if not day.is_working_day:
        return
    ref = _reference(day)
    pending = day.rows("""
        SELECT e.*, s.gst_registration_type, s.gstin AS supplier_gstin FROM expenses e
        LEFT JOIN suppliers s ON s.supplier_id = e.supplier_id
        WHERE e.expense_status = 'SUBMITTED' AND e.expense_type <> 'PAYROLL' ORDER BY e.expense_id""")
    if not pending:
        return
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows(f"""
            SELECT l.*, c.gl_account_id AS account_id, c.category_code FROM expense_line_items l
            JOIN expense_categories c ON c.expense_category_id = l.expense_category_id
            WHERE l.expense_id IN ({", ".join(str(e["expense_id"]) for e in pending)})
            ORDER BY l.expense_id, l.line_number"""):
        lines[line["expense_id"]].append(line)
    book = account_book(day)
    for expense in pending:
        rng = day.streams.stream(expense["received_date"], f"expenses.approval.{expense['expense_id']}")
        wait = 0 if expense["expense_type"] == "PETTY_CASH" else rng.randint(*settings.approval_days)
        if day.day < expense["received_date"] + timedelta(days=wait):
            continue
        at = max(day.at(15, rng.randint(0, 50)), expense["created_at"] + timedelta(minutes=5))
        if expense["vendor_bill_number"] and expense["vendor_bill_number"].startswith("AH") or \
                expense["expense_type"] == "EMPLOYEE_CLAIM":
            if rng.random() < settings.rejection_share:
                day.writer.update("expenses", "expense_id", expense["expense_id"], {
                    "expense_status": "REJECTED", "rejection_reason": rng.choice(REJECTIONS), "updated_at": at})
                continue
        by = approver(day, expense["total_amount"], ("FIN",), day.day)
        _approve(day, ref, book, expense, lines[expense["expense_id"]], by, at)
        if expense["expense_type"] == "PETTY_CASH":
            _pay(day, ref, book, expense, expense["total_amount"], at + timedelta(minutes=10), rng,
                 bank=ref.petty[expense["warehouse_id"]], method="CASH")


def _approve(day: DayContext, ref: _Reference, book, expense: dict[str, Any], lines: list[dict[str, Any]], by: int,
             at: datetime) -> None:
    """P19, AP item and INPUT tax rows."""
    day.writer.update("expenses", "expense_id", expense["expense_id"], {
        "expense_status": "APPROVED", "approved_by_employee_id": by, "approved_at": at, "updated_at": at})
    builder = JournalBuilder(f"Expense {expense['expense_number']}")
    registration = expense["company_gst_registration_id"]
    for line in lines:
        builder.debit(line["account_id"], line["taxable_amount"], cost_center_id=line["cost_center_id"])
        gst = line["cgst_amount"] + line["sgst_amount"] + line["igst_amount"]
        if gst and line["is_itc_eligible"]:
            for component in ("CGST", "SGST", "IGST"):
                builder.debit(book.tax(registration, component, "INPUT"), line[f"{component.lower()}_amount"])
        elif gst:
            builder.debit(book.role("INELIGIBLE_GST"), gst, cost_center_id=line["cost_center_id"])
    party = {"supplier_id": expense["supplier_id"]} if expense["supplier_id"] else {"employee_id": expense["employee_id"]}
    builder.credit(book.role("AP_CONTROL"), expense["total_amount"], **party)
    builder.balance_with(book.role("ROUNDING_OFF"), cost_center_id=ref.departments["FIN"])
    journal_id = post_journal(day, builder.build(), source="EXPENSE", document_date=expense["expense_date"],
                              created_by=by, expense_id=expense["expense_id"])
    day.writer.insert("accounts_payable", {
        "accounts_payable_id": day.ids.next("accounts_payable"), "supplier_id": party.get("supplier_id"),
        "employee_id": party.get("employee_id"), "item_type": "EXPENSE", "supplier_invoice_id": None,
        "expense_id": expense["expense_id"], "supplier_payment_id": None, "document_date": expense["expense_date"],
        "due_date": expense["due_date"], "original_amount": expense["total_amount"],
        "open_amount": expense["total_amount"], "item_status": "OPEN", "settled_at": None,
        "journal_entry_id": journal_id, "created_at": at, "updated_at": at,
    })
    if expense["gst_registration_type"] != "REGISTERED":
        return
    for line in lines:
        if line["gst_rate"] <= 0:
            continue
        day.writer.insert("tax_transactions", {
            "tax_transaction_id": day.ids.next("tax_transactions"), "company_gst_registration_id": registration,
            "tax_direction": "INPUT", "document_type": "EXPENSE", "is_cancellation": False,
            "invoice_line_item_id": None, "credit_note_line_item_id": None, "supplier_invoice_line_item_id": None,
            "expense_line_item_id": line["expense_line_item_id"], "stock_transfer_line_item_id": None,
            "document_number": expense["vendor_bill_number"] or expense["expense_number"],
            "document_date": expense["expense_date"], "counterparty_gstin": expense["supplier_gstin"],
            "customer_id": None, "supplier_id": expense["supplier_id"],
            "place_of_supply_state_id": _bill_state(ref, registration), "is_inter_state": bool(expense["is_inter_state"]),
            "hsn_code": line["sac_code"], "uqc": None, "quantity": None, "taxable_value": line["taxable_amount"],
            "gst_rate": line["gst_rate"], "cgst_amount": line["cgst_amount"], "sgst_amount": line["sgst_amount"],
            "igst_amount": line["igst_amount"],
            "itc_eligibility": "ELIGIBLE" if line["is_itc_eligible"] else "INELIGIBLE", "gstr1_section": None,
            "gst_return_id": None, "created_at": at, "updated_at": at,
        })


def _bill_state(ref: _Reference, registration_id: int) -> int:
    for site in ref.sites.values():
        if site["company_gst_registration_id"] == registration_id:
            return site["state_id"]
    return ref.principal["state_id"]


# ------------------------------------------------------------------------------------------ payments


def _pay(day: DayContext, ref: _Reference, book, expense: dict[str, Any], amount: Decimal, at: datetime,
         rng: random.Random, bank: int, method: str, failed: bool = False, payroll: bool = False) -> None:
    payment_id = day.ids.next("expense_payments")
    registration = (ref.sites[expense["warehouse_id"]] if expense.get("warehouse_id") else ref.principal)
    day.writer.insert("expense_payments", {
        "expense_payment_id": payment_id,
        "payment_number": day.numbers.next(day.conn, "EPAY", day.day, Registration(
            registration["company_gst_registration_id"], registration["state_code"])),
        "expense_id": expense["expense_id"], "payment_date": day.day, "bank_account_id": bank,
        "payment_method_id": ref.methods[method]["payment_method_id"], "amount": amount,
        "bank_reference": None if failed or method == "CASH" else f"XEP{day.day:%y%m%d}{payment_id:08d}",
        "payment_status": "FAILED" if failed else "COMPLETED",
        "failure_reason": rng.choice(PAYMENT_FAILURES) if failed else None, "processed_at": at,
        "created_at": at - timedelta(minutes=5), "updated_at": at,
    })
    if failed:
        return
    builder = JournalBuilder(f"Expense payment {expense['expense_number']}")
    if payroll:
        builder.debit(book.role("SALARIES_PAYABLE"), amount)
    else:
        party = ({"supplier_id": expense["supplier_id"]} if expense["supplier_id"] else
                 {"employee_id": expense["employee_id"]})
        builder.debit(book.role("AP_CONTROL"), amount, **party)
    builder.credit(book.bank(bank), amount)
    post_journal(day, builder.build(), source="EXPENSE_PAYMENT", expense_payment_id=payment_id)
    if not payroll:
        day.execute("""UPDATE accounts_payable SET open_amount = open_amount - :a,
                              item_status = IF(open_amount = 0, 'SETTLED', 'PARTIALLY_SETTLED'),
                              settled_at = IF(open_amount = 0, :t, NULL), updated_at = :t
                       WHERE expense_id = :e""", {"a": amount, "t": at, "e": expense["expense_id"]})


def payment_run(day: DayContext) -> None:
    """Weekly: approved bills and claims due before the next run are paid (NEFT / RTGS) from PAYMENTS (P20)."""
    settings = day.world.config.expenses
    if not day.is_working_day or day.day.weekday() != settings.payment_weekday:
        return
    due = day.rows("""
        SELECT e.*, a.open_amount FROM expenses e JOIN accounts_payable a ON a.expense_id = e.expense_id
        WHERE a.item_status IN ('OPEN', 'PARTIALLY_SETTLED') AND e.expense_type IN ('VENDOR_BILL', 'EMPLOYEE_CLAIM')
          AND e.due_date < :next ORDER BY e.expense_id""", next=day.day + timedelta(days=7))
    if not due:
        return
    ref = _reference(day)
    book = account_book(day)
    ceiling = max_approval(day, ("FIN",), day.day)
    for expense in due:
        rng = day.rng(f"expenses.pay.{expense['expense_id']}")
        amount = expense["open_amount"]
        if amount > ceiling:
            amount = ceiling                       # paid in parts no larger than anyone can approve
        method = "RTGS" if amount >= ref.methods["RTGS"]["min_amount"] else "NEFT"
        _pay(day, ref, book, expense, amount, day.at(16, 30) + timedelta(minutes=rng.randint(10, 120)), rng,
             bank=ref.payments_bank, method=method, failed=rng.random() < settings.payment_failure_share)


# ------------------------------------------------------------------------------------------ payroll


def payroll(day: DayContext) -> None:
    """Month end: the PAYROLL expense (P19 Dr Salaries per cost centre / Cr Salaries Payable, D56)."""
    last = calendar.monthrange(day.day.year, day.day.month)[1]
    if day.day.day != last:
        return
    ref = _reference(day)
    start = day.day.replace(day=1)
    by_centre: dict[int, Decimal] = defaultdict(Decimal)
    for row in day.rows("""
            SELECT employee_id, cost_center_id, monthly_gross_salary, hire_date, termination_date FROM employees
            WHERE hire_date <= :e AND (termination_date IS NULL OR termination_date >= :s) ORDER BY employee_id""",
            s=start, e=day.day):
        first = max(start, row["hire_date"])
        final = min(day.day, row["termination_date"] or day.day)
        days = (final - first).days + 1
        by_centre[row["cost_center_id"]] += money(sql_divide(row["monthly_gross_salary"] * days, last, 2))   # D87
    if not by_centre:
        return
    category = ref.categories["SALARIES"]
    principal = ref.principal
    rng = day.rng("expenses.payroll")
    at = day.at(18, rng.randint(0, 30))
    expense_id = day.ids.next("expenses")
    exact = total(by_centre.values())
    amount = whole_rupees(exact)                      # the header total is whole rupees (ck_expenses_amounts)
    by = approver(day, amount, ("FIN", "HR"), day.day)
    submitter = rng.choice(_staff(day, "HR") or _staff(day, "FIN"))
    number = day.numbers.next(day.conn, "EXP", day.day,
                              Registration(principal["company_gst_registration_id"], principal["state_code"]))
    pay_day = _working_on_or_after(day, day.day + timedelta(days=1))
    day.writer.insert("expenses", {
        "expense_id": expense_id, "expense_number": number, "expense_type": "PAYROLL", "supplier_id": None,
        "employee_id": None, "vendor_bill_number": None, "expense_date": day.day, "received_date": day.day,
        "due_date": pay_day, "service_period_start": start, "service_period_end": day.day,
        "company_gst_registration_id": None, "warehouse_id": None, "is_inter_state": False, "taxable_amount": exact,
        "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO, "round_off_amount": amount - exact,
        "total_amount": amount,
        "itc_eligible_tax_amount": ZERO, "recurrence_key": "PAYROLL", "expense_status": "APPROVED",
        "submitted_by_employee_id": submitter, "approved_by_employee_id": by, "approved_at": at,
        "rejection_reason": None, "description": f"Payroll {day.day:%b %Y}", "created_at": at - timedelta(hours=2),
        "updated_at": at,
    })
    builder = JournalBuilder(f"Payroll {day.day:%b %Y}")
    for number_, (centre, value) in enumerate(sorted(by_centre.items()), 1):
        day.writer.insert("expense_line_items", {
            "expense_line_item_id": day.ids.next("expense_line_items"), "expense_id": expense_id,
            "line_number": number_, "expense_category_id": category["expense_category_id"], "cost_center_id": centre,
            "marketing_campaign_id": None, "description": f"Salaries {day.day:%b %Y}", "sac_code": None,
            "taxable_amount": value, "gst_rate": ZERO, "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO,
            "is_itc_eligible": False, "line_total": value, "created_at": at - timedelta(hours=2),
            "updated_at": at - timedelta(hours=2),                # insert-only (APPEND in the loader, V-LD-02)
        })
        builder.debit(category["account_id"], value, cost_center_id=centre)
    book = account_book(day)
    builder.credit(book.role("SALARIES_PAYABLE"), amount)
    builder.balance_with(book.role("ROUNDING_OFF"), cost_center_id=ref.departments["FIN"])
    post_journal(day, builder.build(), source="EXPENSE", created_by=by, expense_id=expense_id)


def payroll_payments(day: DayContext) -> None:
    """Salaries are paid on the first working day after the month end (P20 Dr Salaries Payable / Cr Bank)."""
    if not day.is_working_day:
        return
    due = day.rows("""
        SELECT e.* FROM expenses e WHERE e.expense_type = 'PAYROLL' AND e.expense_status = 'APPROVED'
          AND e.due_date <= :d AND NOT EXISTS (SELECT 1 FROM expense_payments p WHERE p.expense_id = e.expense_id
                                                 AND p.payment_status = 'COMPLETED')
        ORDER BY e.expense_id""", d=day.day)
    if not due:
        return
    ref = _reference(day)
    book = account_book(day)
    for expense in due:
        rng = day.rng(f"expenses.payroll.pay.{expense['expense_id']}")
        _pay(day, ref, book, expense, expense["total_amount"], day.at(11, rng.randint(0, 59)), rng,
             bank=ref.payroll_bank, method="NEFT", payroll=True)


# ------------------------------------------------------------------------------------------ the day step


def expenses_step(day: DayContext) -> None:
    """Day step 10 (1G §2.2): bills and claims arrive, approvals, petty cash, payments, payroll."""
    ref = _reference(day)
    drafts: list[_Draft] = []
    _site_bills(day, ref, drafts)
    _company_bills(day, ref, drafts)
    _annual_bills(day, ref, drafts)
    _freight_bills(day, ref, drafts)
    _marketplace_bill(day, ref, drafts)
    _campaign_bills(day, ref, drafts)
    _ad_hoc(day, ref, drafts)
    _petty_cash(day, ref, drafts)
    if drafts:
        _staff(day, "FIN")                             # queries before the first buffered row
        rng = day.rng("expenses.record")
        for draft in drafts:
            _record(day, ref, draft, rng)
    payroll_payments(day)
    approvals(day)
    payment_run(day)
    payroll(day)
