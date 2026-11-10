"""Organisation and accounting master data (1B §3.1 levels 0-6; step 3.5a).

Order (each builder only refers to rows planned before it): seed reference tables → company and GST
registrations → warehouse plan → chart of accounts (skeleton + derived) → business units, expense
categories, tax accounts → warehouses and locations → cost centres → bank accounts → employees →
accounting periods. Ids are assigned here (D20); the batch writer inserts parents before children.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.calendar import fiscal_year, fiscal_year_label
from scmgen.core.money import money
from scmgen.domain.identifiers import cin, email, gstin, ifsc, mobile, pan, pincode
from scmgen.generation.master.context import MasterContext, MasterDataError, city_code, domain_slug, trade_name_of

CITY_PIN_SPREAD = 120            # digits after a 3-digit city prefix stay in 001-120
GST_COMPONENT_DIGIT = {"CGST": 1, "SGST": 2, "UTGST": 2, "IGST": 3}


@dataclass
class PlannedWarehouse:
    warehouse_id: int
    code: str
    name: str
    kind: str                       # DISTRIBUTION_CENTRE / RETAIL_STORE
    city: dict[str, Any]
    opened: date
    replenished_by: int | None = None


# ------------------------------------------------------------------------------------------ seed tables


def build_reference(ctx: MasterContext) -> None:
    """Seed reference tables, identical in every workspace (1B §3.2)."""
    tables = [
        ("states", "state_id", ctx.seed.states, "state", "state_code"),
        ("account_types", "account_type_id", ctx.seed.account_types, "account_type", "account_type_code"),
        ("departments", "department_id", ctx.seed.departments, "department", "department_code"),
        ("product_units_of_measure", "unit_of_measure_id", ctx.seed.units_of_measure, "uom", "uom_code"),
        ("payment_methods", "payment_method_id", ctx.seed.payment_methods, "payment_method", "method_code"),
        ("payment_terms", "payment_term_id", ctx.seed.payment_terms, "payment_term", "term_code"),
        ("sales_channels", "sales_channel_id", ctx.seed.sales_channels, "sales_channel", "channel_code"),
        ("marketing_channels", "marketing_channel_id", ctx.seed.marketing_channels, "marketing_channel", "channel_code"),
    ]
    for table, pk, rows, kind, code_column in tables:
        for row in rows:
            row_id = ctx.ids.next(table)
            ctx.ref(kind)[row[code_column]] = row_id
            ctx.add(table, {pk: row_id, **row})
    ctx.refs["state_row"] = {row["state_code"]: row for row in ctx.seed.states}


# ------------------------------------------------------------------------------------------ company


def build_company(ctx: MasterContext) -> None:
    rng, fake = ctx.rng("company"), ctx.faker("company")
    setup = ctx.config.setup
    hq = ctx.city(setup.hq_city)
    dc_states = sorted({ctx.city(c)["state_code"] for c in setup.distribution_centres})
    if hq["state_code"] not in dc_states:
        raise MasterDataError(f"setup.hq_city {setup.hq_city} is in {hq['state_code']}, which has no distribution "
                              "centre: the principal place of business needs a GST registration (D3)")
    trade = trade_name_of(ctx.company_name)
    years = rng.randint(*setup.company_age_years)
    incorporated = ctx.setup_day - timedelta(days=years * 365 + rng.randint(0, 364))
    company_pan = pan(rng, "C", trade)
    company_id = ctx.ids.next("companies")
    slug = domain_slug(trade)
    ctx.refs["company"] = {"id": company_id, "pan": company_pan, "trade_name": trade, "slug": slug,
                           "incorporated": incorporated, "hq_state": hq["state_code"]}
    ctx.add("companies", {
        "company_id": company_id, "company_code": ctx.workspace_code, "legal_name": ctx.company_name[:200],
        "trade_name": trade[:200], "cin": cin(rng, hq["state_code"], incorporated.year), "pan": company_pan,
        "registered_state_id": ctx.ref("state")[hq["state_code"]],
        "registered_address_line1": f"{fake.building_number()}, {fake.street_name()}"[:200],
        "registered_address_line2": None, "registered_city": hq["city"],
        "registered_pincode": pincode(rng, hq["pin_prefix"], CITY_PIN_SPREAD), "incorporation_date": incorporated,
        "base_currency_code": "INR", "fy_start_month": ctx.config.fiscal_year_start_month,
        "email": email("accounts", slug), "phone": mobile(rng), "website": f"https://www.{slug}.example",
    })


def _between(rng, earliest: date, latest: date) -> date:
    return earliest + timedelta(days=rng.randint(0, max(0, (latest - earliest).days)))


def plan_warehouses(ctx: MasterContext) -> list[PlannedWarehouse]:
    """Warehouse ids, codes, names and opening dates (the chart of accounts and GST registrations need
    them before rows are written). The HQ-state DC opens first, soon after incorporation; other DCs open
    over the company's life; each store opens at least six months after the DC that supplies it."""
    rng = ctx.rng("warehouse_plan")
    incorporated = ctx.refs["company"]["incorporated"]
    hq_state = ctx.refs["company"]["hq_state"]
    last_opening = ctx.setup_day - timedelta(days=120)
    taken: set[str] = set()
    plan: list[PlannedWarehouse] = []
    dc_by_city: dict[str, PlannedWarehouse] = {}
    hq_done = False
    for name in ctx.config.setup.distribution_centres:               # DCs first (1B §4.3)
        city = ctx.city(name)
        if city["state_code"] == hq_state and not hq_done:
            opened, hq_done = _between(rng, incorporated + timedelta(days=30), incorporated + timedelta(days=180)), True
        else:
            opened = _between(rng, min(incorporated + timedelta(days=365), last_opening), last_opening - timedelta(days=245))
        code = city_code(name, taken)
        planned = PlannedWarehouse(ctx.ids.next("warehouses"), f"DC-{code}", f"{name} Distribution Centre",
                                   "DISTRIBUTION_CENTRE", city, min(opened, last_opening))
        dc_by_city[name] = planned
        plan.append(planned)
    store_numbers: dict[str, int] = {}
    for name in ctx.config.setup.stores:
        store_numbers[name] = store_numbers.get(name, 0) + 1
        dc = dc_by_city[name]
        number = store_numbers[name]
        opened = _between(rng, min(dc.opened + timedelta(days=180), last_opening), last_opening)
        plan.append(PlannedWarehouse(ctx.ids.next("warehouses"), f"ST-{dc.code[3:]}-{number:02d}",
                                     f"{name} Store {number}", "RETAIL_STORE", dc.city, opened, dc.warehouse_id))
    ctx.refs["warehouses"] = {w.warehouse_id: w for w in plan}
    return plan


def build_registrations(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    """One GSTIN per state with a warehouse (D3). The principal place (HQ state) registers right after
    incorporation; every other state registers shortly before its first warehouse opens."""
    rng = ctx.rng("registrations")
    company = ctx.refs["company"]
    first_opening: dict[str, date] = {}
    for w in warehouses:
        state = w.city["state_code"]
        first_opening[state] = min(first_opening.get(state, w.opened), w.opened)
    for state_code in sorted(first_opening):
        registration_id = ctx.ids.next("company_gst_registrations")
        state = ctx.refs["state_row"][state_code]
        ctx.ref("registration")[state_code] = registration_id
        principal = state_code == company["hq_state"]
        if principal:
            registered = company["incorporated"] + timedelta(days=rng.randint(7, 25))
        else:
            registered = max(first_opening[state_code] - timedelta(days=rng.randint(15, 60)),
                             company["incorporated"] + timedelta(days=7))
        ctx.add("company_gst_registrations", {
            "company_gst_registration_id": registration_id, "company_id": company["id"],
            "state_id": ctx.ref("state")[state_code], "gstin": gstin(state["gst_state_code"], company["pan"]),
            "registration_date": registered, "is_principal_place": principal, "registration_status": "ACTIVE",
            "cancelled_date": None,
        })


# ------------------------------------------------------------------------------------------ accounts


def build_chart_of_accounts(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    """The seed skeleton plus derived accounts per bank account, petty-cash float, GSTIN and BU (1B §5)."""
    by_code: dict[str, dict[str, Any]] = {}
    types = {row["account_type_code"]: row for row in ctx.seed.account_types}

    def add_account(code: str, name: str, type_code: str, parent_code: str | None, postable: bool, *,
                    role: str | None = None, control: str | None = None, normal: str | None = None,
                    cash_flow: str, derived: bool) -> int:
        if code in by_code:
            raise MasterDataError(f"Account code {code} would be used twice (too many derived accounts?)")
        account_id = ctx.ids.next("chart_of_accounts")
        parent = by_code.get(parent_code) if parent_code else None
        account_type = types[type_code]
        by_code[code] = {"id": account_id, "level": parent["level"] + 1 if parent else 1}
        ctx.ref("account")[code] = account_id
        ctx.add("chart_of_accounts", {
            "account_id": account_id, "account_code": code, "account_name": name[:150],
            "account_type_id": ctx.ref("account_type")[type_code],
            "parent_account_id": parent["id"] if parent else None, "account_level": by_code[code]["level"],
            "is_postable": postable, "normal_balance": normal or account_type["normal_balance"],
            "system_role": role, "is_control_account": control is not None, "control_subledger": control,
            "requires_cost_center": postable and account_type["financial_statement"] == "PROFIT_AND_LOSS",
            "is_derived": derived, "cash_flow_category": cash_flow, "description": None, "is_active": True,
        })
        return account_id

    for row in ctx.seed.accounts:                                     # parents before children
        add_account(row["account_code"], row["account_name"], row["account_type_code"], row["parent_account_code"],
                    row["is_postable"], role=row["system_role"], control=row["control_subledger"],
                    normal=row["normal_balance"], cash_flow=row["cash_flow_category"], derived=False)

    templates = ctx.seed.derived_accounts

    def derived(kind: str, seq: int, name: str, component: int | None = None, **names: str) -> int:
        template = templates[kind]
        code = template["code"].format(seq=f"{seq:02d}", component=component if component is not None else "")
        return add_account(code, template["name"].format(name=name, **names), template["type"], template["parent"],
                           True, control=template.get("control"), normal=template.get("normal"),
                           cash_flow=template["cash_flow"], derived=True)

    banks = ctx.seed.banks
    ctx.refs["bank_gl"] = {
        "COLLECTIONS": derived("bank_current", 1, f"{banks[0]['bank_name']} Collections"),
        "PAYMENTS": derived("bank_current", 2, f"{banks[1 % len(banks)]['bank_name']} Payments"),
        "CASH_CREDIT": derived("cash_credit", 1, banks[2 % len(banks)]["bank_name"]),
    }
    ctx.refs["petty_gl"] = {w.warehouse_id: derived("petty_cash", seq, w.name) for seq, w in enumerate(warehouses, 1)}
    for seq, (state_code, registration_id) in enumerate(sorted(ctx.ref("registration").items()), 1):
        state = ctx.refs["state_row"][state_code]
        intra = state["intra_state_tax_component"]
        for direction, kind in (("INPUT", "gst_input"), ("OUTPUT", "gst_output")):
            for component in ("CGST", intra, "IGST"):
                account_id = derived(kind, seq, "", GST_COMPONENT_DIGIT[component], tax=component,
                                     state=state["state_name"])
                ctx.ref("tax_account_gl")[(registration_id, component, direction)] = account_id
    for seq, pack in enumerate(ctx.packs, 1):
        ctx.ref("bu_accounts")[pack.code] = {
            "revenue": derived("bu_revenue", seq, pack.business_unit_name),
            "returns": derived("bu_returns", seq, pack.business_unit_name),
            "cogs": derived("bu_cogs", seq, pack.business_unit_name),
        }


def build_business_units(ctx: MasterContext) -> None:
    company = ctx.refs["company"]
    for pack in ctx.packs:
        bu_id = ctx.ids.next("business_units")
        ctx.ref("business_unit")[pack.code] = bu_id
        accounts = ctx.ref("bu_accounts")[pack.code]
        ctx.add("business_units", {
            "business_unit_id": bu_id, "business_unit_code": pack.business_unit_code,
            "business_unit_name": pack.business_unit_name, "industry_pack_code": pack.code,
            "description": f"{pack.name} business line", "revenue_account_id": accounts["revenue"],
            "sales_returns_account_id": accounts["returns"], "cogs_account_id": accounts["cogs"],
            "launched_date": company["incorporated"] + timedelta(days=30), "is_active": True,
        })


def build_expense_categories(ctx: MasterContext) -> None:
    for row in ctx.seed.expense_categories:
        category_id = ctx.ids.next("expense_categories")
        ctx.ref("expense_category")[row["category_code"]] = category_id
        ctx.add("expense_categories", {
            "expense_category_id": category_id, **{k: v for k, v in row.items() if k != "account"},
            "gl_account_id": ctx.ref("account")[row["account"]],
        })


def build_tax_accounts(ctx: MasterContext) -> None:
    for (registration_id, component, direction), gl_account_id in sorted(ctx.ref("tax_account_gl").items()):
        ctx.add("tax_accounts", {
            "tax_account_id": ctx.ids.next("tax_accounts"), "company_gst_registration_id": registration_id,
            "tax_component": component, "tax_direction": direction, "gl_account_id": gl_account_id,
        })


# ------------------------------------------------------------------------------------------ locations


# Fixed control and flow zones per warehouse type; the rest of the preset's locations are split
# between storage and picking (DC) or sales floor and back-room storage (store). 1B §4.3.
DC_FIXED_ZONES = {"RECEIVING": 4, "QUARANTINE": 4, "DEFECTIVE": 3, "RETURNS": 3, "PACKING": 4, "DISPATCH": 4}
STORE_FIXED_ZONES = {"RECEIVING": 1, "QUARANTINE": 1, "DEFECTIVE": 1, "RETURNS": 1}
ZONE_PREFIX = {"RECEIVING": "RCV", "QUARANTINE": "QRN", "DEFECTIVE": "DEF", "RETURNS": "RET", "PACKING": "PCK",
               "DISPATCH": "DSP", "STORAGE": "STG", "PICKING": "PIK", "SALES_FLOOR": "SFL"}


def build_warehouses(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    rng, fake = ctx.rng("warehouses"), ctx.faker("warehouses")
    volumes = ctx.config.volumes
    for w in warehouses:
        is_dc = w.kind == "DISTRIBUTION_CENTRE"
        state_code = w.city["state_code"]
        jitter = lambda: Decimal(rng.randint(-800, 800)) / 10000          # noqa: E731  (about ±9 km)
        ctx.add("warehouses", {
            "warehouse_id": w.warehouse_id, "warehouse_code": w.code, "warehouse_name": w.name,
            "warehouse_type": w.kind, "company_gst_registration_id": ctx.ref("registration")[state_code],
            "state_id": ctx.ref("state")[state_code],
            "address_line1": (f"Plot {rng.randint(1, 250)}, {fake.street_name()} Industrial Area" if is_dc
                              else f"{fake.building_number()}, {fake.street_name()}")[:200],
            "address_line2": None, "city": w.city["city"], "pincode": pincode(rng, w.city["pin_prefix"], CITY_PIN_SPREAD),
            "latitude": (w.city["latitude"] + jitter()).quantize(Decimal("0.000001")),
            "longitude": (w.city["longitude"] + jitter()).quantize(Decimal("0.000001")),
            "storage_area_sqft": rng.randint(60_000, 150_000) if is_dc else rng.randint(2_000, 5_000),
            "replenishment_warehouse_id": w.replenished_by, "opened_date": w.opened, "closed_date": None,
            "warehouse_status": "ACTIVE",
        })
        _build_locations(ctx, rng, w, volumes.locations_per_dc if is_dc else volumes.locations_per_store)


def _build_locations(ctx: MasterContext, rng, w: PlannedWarehouse, target: int) -> None:
    is_dc = w.kind == "DISTRIBUTION_CENTRE"
    fixed = DC_FIXED_ZONES if is_dc else STORE_FIXED_ZONES
    remaining = max(target - sum(fixed.values()), 2)
    first_share = round(remaining * (0.4 if is_dc else 0.7))
    racked = {("STORAGE" if is_dc else "SALES_FLOOR"): first_share,
              ("PICKING" if is_dc else "STORAGE"): remaining - first_share}

    def add(zone: str, code: str, location_type: str, capacity: int, pickable: bool,
            aisle: str | None = None, rack: str | None = None, level: str | None = None, bin_: str | None = None) -> None:
        ctx.add("warehouse_locations", {
            "warehouse_location_id": ctx.ids.next("warehouse_locations"), "warehouse_id": w.warehouse_id,
            "location_code": code, "zone": zone, "aisle": aisle, "rack": rack, "shelf_level": level, "bin": bin_,
            "location_type": location_type, "capacity_units": capacity, "is_pickable": pickable, "is_active": True,
            "deleted_at": None,
        })

    for zone, count in fixed.items():
        for n in range(1, count + 1):
            add(zone, f"{ZONE_PREFIX[zone]}-{n:02d}", "FLOOR", rng.randint(200, 800), False)
    for zone, count in racked.items():
        location_type = {"STORAGE": "PALLET" if is_dc else "SHELF", "PICKING": "BIN", "SALES_FLOOR": "SHELF"}[zone]
        per_rack = 4 if location_type == "PALLET" else 8
        for n in range(count):
            aisle, rack_index = divmod(n // per_rack, 10)
            level = n % per_rack + 1
            aisle_code, rack_code = f"A{aisle + 1:02d}", f"R{rack_index + 1:02d}"
            add(zone, f"{ZONE_PREFIX[zone]}-{aisle_code}-{rack_code}-L{level}", location_type,
                rng.randint(30, 60) if location_type == "PALLET" else rng.randint(40, 200),
                zone in ("PICKING", "SALES_FLOOR"), aisle_code, rack_code, f"L{level}")


# ------------------------------------------------------------------------------------------ cost centres


def build_cost_centers(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    """CORP root + one per business unit, warehouse and department, all children of CORP (1B §4.9)."""
    corp = ctx.ids.next("cost_centers")
    ctx.ref("cost_center")["CORP"] = corp

    def add(cc_id: int, code: str, name: str, kind: str, parent: int | None, **link: int | None) -> None:
        ctx.add("cost_centers", {"cost_center_id": cc_id, "cost_center_code": code, "cost_center_name": name[:100],
                                 "cost_center_type": kind, "business_unit_id": link.get("business_unit_id"),
                                 "warehouse_id": link.get("warehouse_id"), "department_id": link.get("department_id"),
                                 "parent_cost_center_id": parent, "is_active": True})

    add(corp, "CORP", "Corporate", "CORPORATE", None)
    for pack in ctx.packs:
        cc = ctx.ids.next("cost_centers")
        ctx.ref("cost_center")[("BU", pack.code)] = cc
        add(cc, f"BU-{pack.business_unit_code}", f"Business Unit - {pack.business_unit_name}", "BUSINESS_UNIT", corp,
            business_unit_id=ctx.ref("business_unit")[pack.code])
    for w in warehouses:
        cc = ctx.ids.next("cost_centers")
        ctx.ref("cost_center")[("WH", w.warehouse_id)] = cc
        add(cc, f"WH-{w.code}", f"Warehouse - {w.name}", "WAREHOUSE", corp, warehouse_id=w.warehouse_id)
    for row in ctx.seed.departments:
        cc = ctx.ids.next("cost_centers")
        ctx.ref("cost_center")[("DEPT", row["department_code"])] = cc
        add(cc, f"DEPT-{row['department_code']}", f"Department - {row['department_name']}", "DEPARTMENT", corp,
            department_id=ctx.ref("department")[row["department_code"]])


# ------------------------------------------------------------------------------------------ bank accounts


def build_bank_accounts(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    """One collections and one payments/payroll current account, one cash-credit account at HQ, and a
    petty-cash float per warehouse (1B §4.8). No opening balances here (D22)."""
    rng = ctx.rng("bank_accounts")
    banks = ctx.seed.banks
    company = ctx.refs["company"]
    opened = company["incorporated"] + timedelta(days=rng.randint(5, 40))
    branch = f"{ctx.config.setup.hq_city} Main Branch"
    numbers: set[str] = set()

    def account_number() -> str:
        while True:
            value = f"{rng.randint(1, 9)}{rng.randint(0, 10**13 - 1):013d}"
            if value not in numbers:
                numbers.add(value)
                return value

    accounts = [
        ("BNK-COLL-01", "Collections Account", "CURRENT", "COLLECTIONS", banks[0], ctx.refs["bank_gl"]["COLLECTIONS"], None),
        ("BNK-PAY-01", "Payments and Payroll Account", "CURRENT", "PAYMENTS", banks[1 % len(banks)],
         ctx.refs["bank_gl"]["PAYMENTS"], None),
        ("BNK-CC-01", "Cash Credit Account", "CASH_CREDIT", "GENERAL", banks[2 % len(banks)],
         ctx.refs["bank_gl"]["CASH_CREDIT"], money(ctx.config.setup.cash_credit_limit)),
    ]
    for code, name, account_type, purpose, bank, gl, limit in accounts:
        bank_account_id = ctx.ids.next("bank_accounts")
        ctx.ref("bank_account")[purpose] = bank_account_id
        ctx.add("bank_accounts", {
            "bank_account_id": bank_account_id, "account_code": code, "account_name": name,
            "account_type": account_type, "account_purpose": purpose, "bank_name": bank["bank_name"],
            "branch_name": branch, "ifsc_code": ifsc(rng, bank["bank_code"]), "account_number": account_number(),
            "currency_code": "INR", "overdraft_limit": limit, "warehouse_id": None, "gl_account_id": gl,
            "opened_date": opened, "account_status": "ACTIVE",
        })
    for w in warehouses:
        bank_account_id = ctx.ids.next("bank_accounts")
        ctx.ref("petty_cash")[w.warehouse_id] = bank_account_id
        ctx.add("bank_accounts", {
            "bank_account_id": bank_account_id, "account_code": f"PETTY-{w.code}"[:20],
            "account_name": f"Petty Cash - {w.name}"[:100], "account_type": "PETTY_CASH",
            "account_purpose": "PETTY_CASH", "bank_name": None, "branch_name": None, "ifsc_code": None,
            "account_number": None, "currency_code": "INR", "overdraft_limit": None, "warehouse_id": w.warehouse_id,
            "gl_account_id": ctx.refs["petty_gl"][w.warehouse_id], "opened_date": opened, "account_status": "ACTIVE",
        })


# ------------------------------------------------------------------------------------------ employees


def _staffing(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> tuple[dict[int, int], dict[int, int], dict[str, int]]:
    """Staff counts per DC (leads), per warehouse (staff) and per central department, from the preset target."""
    org = ctx.config.organisation
    dcs = [w for w in warehouses if w.kind == "DISTRIBUTION_CENTRE"]
    stores = [w for w in warehouses if w.kind == "RETAIL_STORE"]
    rng = ctx.rng("staffing")
    # Fixed roles: CEO, department heads (+ warehouse operations), DC and store managers.
    fixed = 1 + len(org.departments) + 1 + len(dcs) + len(stores)
    remaining = max(ctx.config.volumes.employees - fixed, 0)
    warehouse_staff = round(remaining * org.warehouse_staff_share)
    store_total = round(warehouse_staff * 0.15) if stores else 0
    per_dc = max(2, (warehouse_staff - store_total) // max(len(dcs), 1))
    # Leads come out of the DC's headcount: one for a small DC, two or three from ten people up (1B §4.2).
    leads = {w.warehouse_id: 1 if per_dc < 10 else rng.choice((2, 3)) for w in dcs}
    staff = {w.warehouse_id: max(1, per_dc - leads[w.warehouse_id]) for w in dcs}
    staff |= {w.warehouse_id: max(1, store_total // len(stores)) for w in stores}
    central = remaining - warehouse_staff
    weights = sum(d.weight for d in org.departments)
    central_staff = {d.code: max(1, round(central * d.weight / weights)) for d in org.departments}
    return leads, staff, central_staff


def build_employees(ctx: MasterContext, warehouses: list[PlannedWarehouse]) -> None:
    """Top-down: CEO → department heads → DC managers → leads → staff; store managers → staff (1B §4.2)."""
    org = ctx.config.organisation
    rng, fake = ctx.rng("employees"), ctx.faker("employees")
    for d in org.departments:
        if d.code not in ctx.ref("department"):
            raise MasterDataError(f"organisation.departments.{d.code} is not a seed department")
    company = ctx.refs["company"]
    emails: set[str] = set()
    number = 0

    def _hired(proposed: date, warehouse_id: int | None, level: str) -> date:
        if warehouse_id is None:
            return proposed
        opened = ctx.refs["warehouses"][warehouse_id].opened
        not_before = opened - timedelta(days=30) if level == "MANAGER" else opened
        if proposed >= not_before:
            return proposed
        return _between(rng, not_before, max(not_before, ctx.setup_day - timedelta(days=7)))

    def hire_date(min_years: float, max_years: float) -> date:
        earliest = max(company["incorporated"] + timedelta(days=15), ctx.setup_day - timedelta(days=int(max_years * 365)))
        latest = ctx.setup_day - timedelta(days=int(min_years * 365) + 7)
        return earliest + timedelta(days=rng.randint(0, max(0, (latest - earliest).days)))

    def hire(department: str, level: str, title: str, manager: int | None, warehouse_id: int | None = None,
             years: tuple[float, float] = (0.2, 6)) -> int:
        """Hire one employee; warehouse staff are hired after their warehouse opens (managers up to a month before)."""
        nonlocal number
        number += 1
        first, last = fake.first_name(), fake.last_name()
        address = email(f"{first}.{last}", company["slug"])
        suffix = 1
        while address in emails:
            suffix += 1
            address = email(f"{first}.{last}{suffix}", company["slug"])
        emails.add(address)
        band = org.job_levels[level]
        salary = Decimal(rng.randint(band.salary[0] // 500, band.salary[1] // 500) * 500)
        employee_id = ctx.ids.next("employees")
        cost_center = ctx.ref("cost_center")[("WH", warehouse_id) if warehouse_id else ("DEPT", department)]
        ctx.ref("employees_by_department").setdefault(department, []).append(employee_id)
        if department == "SAL" and warehouse_id is None and level == "STAFF":
            ctx.refs.setdefault("sales_executives", []).append(employee_id)       # B2B account managers
        ctx.add("employees", {
            "employee_id": employee_id, "employee_code": f"EMP-{number:05d}", "first_name": first[:60],
            "last_name": last[:60], "email": address, "phone": mobile(rng),
            "department_id": ctx.ref("department")[department], "warehouse_id": warehouse_id,
            "cost_center_id": cost_center, "manager_employee_id": manager, "job_title": title[:100],
            "job_level": level, "approval_limit": money(band.approval_limit), "monthly_gross_salary": money(salary),
            "hire_date": _hired(hire_date(*years), warehouse_id, level), "termination_date": None,
            "employment_status": "ACTIVE",
            "deleted_at": None,
        })
        return employee_id

    leads, staff, central_staff = _staffing(ctx, warehouses)
    ceo = hire("MGMT", "EXECUTIVE", org.chief_executive, None, years=(3, 12))
    heads = {d.code: hire(d.code, "HEAD", d.head, ceo, years=(1, 10)) for d in org.departments}
    who = org.warehouse_titles["WHO"]
    heads["WHO"] = hire("WHO", "HEAD", who["head"], ceo, years=(1, 10))
    for d in org.departments:                     # central departments: a manager from 3 staff up
        manager = hire(d.code, "MANAGER", d.manager, heads[d.code], years=(0.5, 8)) \
            if central_staff[d.code] >= 3 else heads[d.code]
        for _ in range(central_staff[d.code]):
            hire(d.code, "STAFF", d.staff, manager)
    store_titles = org.warehouse_titles["store"]
    for w in warehouses:
        if w.kind == "DISTRIBUTION_CENTRE":
            manager = hire("WHO", "MANAGER", who["dc_manager"], heads["WHO"], w.warehouse_id, years=(0.5, 8))
            lead_ids = [hire("WHO", "LEAD", who["lead"], manager, w.warehouse_id, years=(0.3, 6))
                        for _ in range(leads[w.warehouse_id])]
            for n in range(staff[w.warehouse_id]):
                hire("WHO", "STAFF", who["staff"], lead_ids[n % len(lead_ids)], w.warehouse_id)
        else:
            manager = hire("SAL", "MANAGER", store_titles["manager"], heads["SAL"], w.warehouse_id, years=(0.3, 6))
            for _ in range(staff[w.warehouse_id]):
                hire("SAL", "STAFF", store_titles["staff"], manager, w.warehouse_id)


# ------------------------------------------------------------------------------------------ periods


def build_accounting_periods(ctx: MasterContext) -> None:
    """Every month from the start of the FY holding the first simulated day to the end of the FY holding
    the last (1B §4.9). All FUTURE: the daily engine opens and closes them (step 3.6)."""
    start_month = ctx.config.fiscal_year_start_month
    first_fy = fiscal_year(ctx.config.start_date, start_month)
    last_fy = fiscal_year(ctx.config.end_date, start_month)
    for fy in range(first_fy, last_fy + 1):
        for period_number in range(1, 13):
            month_index = start_month - 1 + period_number - 1
            year, month = fy + month_index // 12, month_index % 12 + 1
            start = date(year, month, 1)
            end = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
            ctx.add("accounting_periods", {
                "accounting_period_id": ctx.ids.next("accounting_periods"), "fiscal_year": fy,
                "fiscal_year_label": fiscal_year_label(fy, start_month), "period_number": period_number,
                "period_code": f"{year}-{month:02d}", "period_name": start.strftime("%b %Y"),
                "start_date": start, "end_date": end, "period_status": "FUTURE", "opened_at": None,
                "closed_at": None, "closed_by_employee_id": None,
            })


def build_organisation(ctx: MasterContext) -> None:
    """Step 3.5a: everything up to employees and periods, in dependency order."""
    build_reference(ctx)
    build_company(ctx)
    warehouses = plan_warehouses(ctx)
    build_registrations(ctx, warehouses)
    build_chart_of_accounts(ctx, warehouses)
    build_business_units(ctx)
    build_expense_categories(ctx)
    build_tax_accounts(ctx)
    build_warehouses(ctx, warehouses)
    build_cost_centers(ctx, warehouses)
    build_bank_accounts(ctx, warehouses)
    build_employees(ctx, warehouses)
    build_accounting_periods(ctx)
