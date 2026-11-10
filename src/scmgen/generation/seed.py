"""Seed reference data: load ``config/seed/*.yaml`` and check it (1B §3.2 "Seed", §5).

Rows come back as dicts keyed by column name (values already converted: decimals to ``Decimal``), so
master-data generation only adds ids and timestamps. Checks here cover what the database can't see
on its own: the role list is complete, the account hierarchy and code rules hold, cities and expense
categories reference existing rows, identifiers are visibly synthetic. Column formats and enumerations
are enforced by the DDL itself (an integration test inserts the whole seed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.domain.journal import SYSTEM_ROLES

SEED_DIR = "seed"
DECIMAL_COLUMNS = {"gateway_fee_percent", "min_amount", "max_amount", "early_payment_discount_percent",
                   "commission_percent", "default_gst_rate", "latitude", "longitude"}
DERIVED_TEMPLATES = {"bank_current", "petty_cash", "cash_credit", "gst_input", "gst_output",
                     "bu_revenue", "bu_returns", "bu_cogs"}
# Optional columns per seed table and the value used when the YAML leaves them out (the DDL's own
# defaults). Every other column of a row is required, so all rows of a table end up with the same
# columns, which the batch writer needs.
OPTIONAL_COLUMNS: dict[str, dict[str, Any]] = {
    "states": {"is_union_territory": False, "intra_state_tax_component": "SGST"},
    "departments": {"is_active": True},
    "units_of_measure": {},
    "account_types": {},
    "payment_methods": {"settlement_lag_days": 0, "gateway_fee_percent": None, "min_amount": None,
                        "max_amount": None, "is_active": True},
    "payment_terms": {"is_prepaid": False, "early_payment_discount_percent": None,
                      "early_payment_discount_days": None, "is_active": True},
    "sales_channels": {"commission_percent": None, "is_active": True},
    "marketing_channels": {"is_active": True},
    "shipping_carriers": {"supports_cod": False, "supports_reverse_pickup": False,
                          "tracking_url_template": None, "is_active": True},
    "banks": {},
    "cities": {},
    "expense_categories": {"default_gst_rate": None, "is_active": True},
}
_ACCOUNT_KEYS = {"code", "name", "type", "parent", "postable", "role", "control", "normal", "cash_flow"}


class SeedError(ConfigError):
    """The seed data is inconsistent; the message lists every problem found."""


@dataclass(frozen=True)
class SeedData:
    states: tuple[dict[str, Any], ...]
    cities: tuple[dict[str, Any], ...]
    account_types: tuple[dict[str, Any], ...]
    departments: tuple[dict[str, Any], ...]
    units_of_measure: tuple[dict[str, Any], ...]
    payment_methods: tuple[dict[str, Any], ...]
    payment_terms: tuple[dict[str, Any], ...]
    sales_channels: tuple[dict[str, Any], ...]
    marketing_channels: tuple[dict[str, Any], ...]
    shipping_carriers: tuple[dict[str, Any], ...]
    banks: tuple[dict[str, Any], ...]
    accounts: tuple[dict[str, Any], ...]           # COA skeleton, parents before children
    derived_accounts: Mapping[str, dict[str, Any]]
    expense_categories: tuple[dict[str, Any], ...]

    def state(self, state_code: str) -> dict[str, Any]:
        for row in self.states:
            if row["state_code"] == state_code:
                return row
        raise KeyError(state_code)

    def account(self, code: str) -> dict[str, Any]:
        for row in self.accounts:
            if row["account_code"] == code:
                return row
        raise KeyError(code)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SeedError(f"Seed file not found: {path}")
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SeedError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise SeedError(f"{path} must contain a mapping at the top level")
    return content


def _rows(content: Mapping[str, Any], key: str, source: str, problems: list[str]) -> tuple[dict[str, Any], ...]:
    """Rows of one seed table, completed to a uniform column set (``OPTIONAL_COLUMNS`` defaults)."""
    rows = content.get(key)
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise SeedError(f"{source}: '{key}' must be a non-empty list of mappings")
    optional = OPTIONAL_COLUMNS[key]
    required = {column for row in rows for column in row} - set(optional)
    completed = []
    for index, row in enumerate(rows, 1):
        missing = sorted(required - set(row))
        if missing:
            problems.append(f"{source} {key} row {index}: missing {', '.join(missing)}")
        full = {**optional, **row}
        completed.append({k: (Decimal(str(v)) if k in DECIMAL_COLUMNS and v is not None else v)
                          for k, v in sorted(full.items())})
    return tuple(completed)


def _unique(problems: list[str], rows: tuple[dict[str, Any], ...], key: str, table: str) -> None:
    seen: set[Any] = set()
    for row in rows:
        value = row.get(key)
        if value in seen:
            problems.append(f"{table}: duplicate {key} {value!r}")
        seen.add(value)


def _build_accounts(raw: tuple[dict[str, Any], ...], types: Mapping[str, dict[str, Any]],
                    problems: list[str]) -> tuple[dict[str, Any], ...]:
    """COA skeleton rows (table columns), checking hierarchy, codes and roles."""
    by_code: dict[str, dict[str, Any]] = {}
    roles: list[str] = []
    for entry in raw:
        unknown = set(entry) - _ACCOUNT_KEYS
        if unknown:
            problems.append(f"account {entry.get('code')}: unknown field(s) {sorted(unknown)}")
        code, type_code = str(entry.get("code")), entry.get("type")
        account_type = types.get(type_code)
        if account_type is None:
            problems.append(f"account {code}: unknown type {type_code!r}")
            continue
        if not re.fullmatch(r"[1-6][0-9]{4}", code) or code[0] != account_type["code_prefix"]:
            problems.append(f"account {code}: code must be 5 digits starting with {account_type['code_prefix']} ({type_code})")
        parent_code = entry.get("parent")
        level = 1
        if parent_code is not None:
            parent = by_code.get(str(parent_code))
            if parent is None:
                problems.append(f"account {code}: parent {parent_code} must be listed before it")
                continue
            if parent["account_type_code"] != type_code:
                problems.append(f"account {code}: type {type_code} differs from parent {parent_code}")
            if parent["is_postable"]:
                problems.append(f"account {code}: parent {parent_code} is postable; parents must be headers")
            level = parent["account_level"] + 1
        if level > 4:
            problems.append(f"account {code}: deeper than 4 levels")
        postable = bool(entry.get("postable"))
        role, control = entry.get("role"), entry.get("control")
        if not postable and (role or control):
            problems.append(f"account {code}: a header account carries no role or control subledger")
        if role:
            roles.append(role)
        is_pnl = account_type["financial_statement"] == "PROFIT_AND_LOSS"
        by_code[code] = {
            "account_code": code, "account_name": entry.get("name"), "account_type_code": type_code,
            "parent_account_code": None if parent_code is None else str(parent_code), "account_level": level,
            "is_postable": postable, "normal_balance": entry.get("normal") or account_type["normal_balance"],
            "system_role": role, "is_control_account": control is not None, "control_subledger": control,
            "requires_cost_center": postable and is_pnl, "is_derived": False,
            "cash_flow_category": entry.get("cash_flow"),
        }
        if is_pnl and entry.get("cash_flow") != "NOT_APPLICABLE":
            problems.append(f"account {code}: P&L accounts have cash_flow NOT_APPLICABLE (profit enters as net profit, D78)")
    duplicates = sorted({r for r in roles if roles.count(r) > 1})
    if duplicates:
        problems.append(f"system roles used more than once: {', '.join(duplicates)}")
    missing, unknown_roles = SYSTEM_ROLES - set(roles), set(roles) - SYSTEM_ROLES
    if missing:
        problems.append(f"system roles without an account (V-AC-04): {', '.join(sorted(missing))}")
    if unknown_roles:
        problems.append(f"roles the posting engine doesn't know: {', '.join(sorted(unknown_roles))}")
    if len(by_code) != len(raw):
        problems.append("chart_of_accounts: duplicate account codes")
    return tuple(by_code.values())


def load_seed(config_dir: Path = DEFAULT_CONFIG_DIR) -> SeedData:
    """Load and check all seed files; raises ``SeedError`` listing every problem."""
    directory = config_dir / SEED_DIR
    states_file = _read(directory / "states.yaml")
    cities_file = _read(directory / "cities.yaml")
    reference = _read(directory / "reference.yaml")
    coa = _read(directory / "chart_of_accounts.yaml")
    problems: list[str] = []

    states = _rows(states_file, "states", "states.yaml", problems)
    cities = _rows(cities_file, "cities", "cities.yaml", problems)
    tables = {key: _rows(reference, key, "reference.yaml", problems) for key in (
        "account_types", "departments", "units_of_measure", "payment_methods", "payment_terms",
        "sales_channels", "marketing_channels", "shipping_carriers", "banks")}

    for key, column in [("states", "gst_state_code"), ("states", "state_code"), ("states", "state_name")]:
        _unique(problems, states, column, key)
    for table, column in [("account_types", "account_type_code"), ("departments", "department_code"),
                          ("units_of_measure", "uom_code"), ("payment_methods", "method_code"),
                          ("payment_terms", "term_code"), ("sales_channels", "channel_code"),
                          ("marketing_channels", "channel_code"), ("shipping_carriers", "carrier_code"),
                          ("shipping_carriers", "tracking_number_prefix"), ("banks", "bank_code")]:
        _unique(problems, tables[table], column, table)

    state_codes = {row["state_code"] for row in states}
    seen_cities: set[tuple[str, str]] = set()
    for city in cities:
        name, state = city.get("city"), city.get("state_code")
        if state not in state_codes or state == "OT":
            problems.append(f"city {name}: unknown or unusable state {state!r}")
        if (name, state) in seen_cities:
            problems.append(f"city {name} ({state}) listed twice")
        seen_cities.add((name, state))
        if not re.fullmatch(r"[1-9][0-9]{2}", str(city.get("pin_prefix"))):
            problems.append(f"city {name}: pin_prefix must be 3 digits, got {city.get('pin_prefix')!r}")
        lat, lon = city.get("latitude"), city.get("longitude")
        if lat is None or lon is None or not (Decimal(6) <= lat <= Decimal(38) and Decimal(68) <= lon <= Decimal(98)):
            problems.append(f"city {name}: coordinates {lat}, {lon} are outside India")
        if not isinstance(city.get("weight"), int) or city["weight"] < 1:
            problems.append(f"city {name}: weight must be a positive whole number")
    missing_states = {"KA", "MH", "DL", "TN", "TG", "WB", "GJ"} - {c.get("state_code") for c in cities}
    if missing_states:
        problems.append(f"cities.yaml has no city in major states {sorted(missing_states)}")

    for bank in tables["banks"]:
        if not re.fullmatch(r"X[A-Z]{3}", str(bank.get("bank_code"))):
            problems.append(f"bank {bank.get('bank_name')}: invented IFSC codes start with X (D24), got {bank.get('bank_code')!r}")
    for carrier in tables["shipping_carriers"]:
        url = carrier.get("tracking_url_template")
        if url is not None and ("{awb}" not in url or ".example" not in url):
            problems.append(f"carrier {carrier.get('carrier_code')}: tracking URL needs {{awb}} and a .example domain (D24)")

    types = {row["account_type_code"]: row for row in tables["account_types"]}
    accounts = _build_accounts(tuple(coa.get("accounts") or ()), types, problems)
    account_by_code = {row["account_code"]: row for row in accounts}

    derived = coa.get("derived") or {}
    if set(derived) != DERIVED_TEMPLATES:
        problems.append(f"derived account templates must be exactly {sorted(DERIVED_TEMPLATES)}, got {sorted(derived)}")
    for name, template in derived.items():
        parent = account_by_code.get(str(template.get("parent")))
        if parent is None or parent["is_postable"] or parent["account_type_code"] != template.get("type"):
            problems.append(f"derived template {name}: parent must be a header account of type {template.get('type')}")
        if "{seq}" not in str(template.get("code")):
            problems.append(f"derived template {name}: code needs a {{seq}} placeholder")

    expense_categories = _rows(coa, "expense_categories", "chart_of_accounts.yaml", problems)
    for table, column in [("expense_categories", "category_code"), ("expense_categories", "category_name")]:
        _unique(problems, expense_categories, column, table)
    for category in expense_categories:
        account = account_by_code.get(str(category.get("account")))
        if account is None or not account["is_postable"] or account["account_type_code"] not in ("EXPENSE", "COGS"):
            problems.append(f"expense category {category.get('category_code')}: account {category.get('account')} "
                            "must be a postable EXPENSE or COGS account")
        rate = category.get("default_gst_rate")
        if category.get("is_itc_eligible") and not (rate and rate > 0):
            problems.append(f"expense category {category.get('category_code')}: input credit needs a GST rate > 0")

    if problems:
        raise SeedError("Seed data problems:\n  - " + "\n  - ".join(problems))
    return SeedData(states=states, cities=cities, accounts=accounts, derived_accounts=dict(derived),
                    expense_categories=expense_categories, **tables)
