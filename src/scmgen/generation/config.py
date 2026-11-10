"""Generation configuration: presets, overrides, resolved dates, snapshot and hash.

``config/generation.yaml`` holds defaults and the Small / Medium / Large presets. A run resolves them
into one ``GenerationConfig``. Its ``snapshot()`` is stored in ``generation_runs.config_snapshot``
with a SHA-256 ``config_hash``, so every run records exactly what produced its data (1G §5.2).
"""

from __future__ import annotations

import copy
from calendar import monthrange
import hashlib
import json
import platform
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from scmgen import __version__
from scmgen.core.calendar import today_ist
from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR

GENERATION_FILE = "generation.yaml"
MAX_HISTORY_DAYS = 3 * 366 + 1           # design: 1-3 years of history (1G §3)

_DEFAULT_KEYS = {"preset", "fiscal_year_start_month", "batch_size", "history", "gst_rate_revision"}
_HISTORY_KEYS = {"start_date", "end_date", "allow_future"}
_PRESET_KEYS = {"history_months", "volumes"}


@dataclass(frozen=True)
class Volumes:
    customers_b2b: int
    customers_b2c: int
    suppliers: int
    products: int
    employees: int
    locations_per_dc: int
    locations_per_store: int
    orders_per_day: int


_VOLUME_KEYS = set(Volumes.__dataclass_fields__)
_SETUP_KEYS = {"hq_city", "distribution_centres", "stores", "company_age_years", "cash_credit_limit"}
_MASTER_SCHEMA = {
    "customers": {"b2b_segments", "b2b_registration", "credit_limits", "name_suffixes",
                  "extra_shipping_address_share", "campaign_attribution", "acquisition_mean_years",
                  "warehouse_region_weight"},
    "suppliers": {"goods_registration", "msme_share", "dispatch_address_share", "per_product"},
    "campaigns": {"history_years", "cancelled_share", "budget"},
}
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
_DAILY_KEYS = {"weekly_off", "national_holidays", "period_close_day", "progress_every_days", "new_products_per_year",
               "discontinued_per_year", "campaign_lead_days"}
_OPENING_KEYS = {"share_capital", "bank_balances", "cash_credit_drawn", "petty_cash", "fixed_assets",
                 "accumulated_depreciation_share", "store_assortment_share"}
_PROCUREMENT_KEYS = {"direct_reorder_max_value", "rfq_max_lines", "rfq_suppliers", "rfq_response_days", "response_mix",
                     "quote_price_factor", "quote_lead_factor", "quote_validity_days", "evaluation_days",
                     "award_price_ceiling", "score_weights", "approval_days", "po_cancel_share"}
B2B_SEGMENTS = ("RETAILER", "WHOLESALER", "CORPORATE", "INSTITUTION", "WORKSHOP")
_ORG_KEYS = {"job_levels", "warehouse_staff_share", "departments", "warehouse_titles", "chief_executive"}
JOB_LEVELS = ("EXECUTIVE", "HEAD", "MANAGER", "LEAD", "STAFF")


@dataclass(frozen=True)
class Setup:
    """Company identity inputs (1B §3.2 "Setup"); city names are checked against the seed by the builder."""

    hq_city: str
    distribution_centres: tuple[str, ...]
    stores: tuple[str, ...]
    company_age_years: tuple[int, int]
    cash_credit_limit: int


@dataclass(frozen=True)
class JobLevel:
    salary: tuple[int, int]
    approval_limit: int


@dataclass(frozen=True)
class Department:
    code: str
    weight: float
    head: str
    manager: str
    staff: str


@dataclass(frozen=True)
class MasterDataSettings:
    """Behaviour of master-data generation (1B §4.4-§4.7); see ``master_data`` in generation.yaml."""

    b2b_segments: dict[str, float]
    b2b_registration: dict[str, float]
    credit_limits: dict[str, tuple[int, int]]
    customer_name_suffixes: dict[str, tuple[str, ...]]
    extra_shipping_address_share: float
    campaign_attribution: dict[str, float]
    acquisition_mean_years: float
    warehouse_region_weight: float
    supplier_registration: dict[str, float]
    msme_share: float
    dispatch_address_share: float
    suppliers_per_product: tuple[int, int]
    campaign_history_years: int
    campaign_cancelled_share: float
    campaign_budget: tuple[int, int]


@dataclass(frozen=True)
class DailySettings:
    weekly_off: tuple[int, ...]                  # date.weekday() numbers (Monday = 0)
    national_holidays: tuple[tuple[int, int], ...]   # (month, day)
    period_close_day: int
    progress_every_days: int
    new_products_per_year: float
    discontinued_per_year: float
    campaign_lead_days: int

    def is_working_day(self, day: date) -> bool:
        return day.weekday() not in self.weekly_off and (day.month, day.day) not in self.national_holidays


@dataclass(frozen=True)
class OpeningBalances:
    share_capital: int
    bank_balances: dict[str, int]                 # by bank account purpose
    cash_credit_drawn: int
    petty_cash: dict[str, int]                    # by warehouse type
    fixed_assets: dict[str, int]                  # account code → cost
    accumulated_depreciation_share: float
    store_assortment_share: float


@dataclass(frozen=True)
class ProcurementSettings:
    direct_reorder_max_value: int
    rfq_max_lines: int
    rfq_suppliers: tuple[int, int]
    rfq_response_days: tuple[int, int]
    response_mix: dict[str, float]
    quote_price_factor: tuple[float, float]
    quote_lead_factor: tuple[float, float]
    quote_validity_days: tuple[int, int]
    evaluation_days: tuple[int, int]
    award_price_ceiling: float
    score_weights: dict[str, float]
    approval_days: tuple[int, int]
    po_cancel_share: float


@dataclass(frozen=True)
class Organisation:
    job_levels: dict[str, JobLevel]
    warehouse_staff_share: float
    departments: tuple[Department, ...]
    warehouse_titles: dict[str, dict[str, str]]
    chief_executive: str


@dataclass(frozen=True)
class GenerationConfig:
    preset: str
    start_date: date
    end_date: date
    allow_future: bool
    fiscal_year_start_month: int
    batch_size: int
    volumes: Volumes
    gst_rate_revision: bool = True        # apply the packs' 22 Sep 2025 rate change (D17)
    setup: Setup | None = None
    organisation: Organisation | None = None
    master_data: MasterDataSettings | None = None
    daily: DailySettings | None = None
    opening_balances: OpeningBalances | None = None
    procurement: ProcurementSettings | None = None

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready record of everything that shapes the generated data, incl. the versions it depends on."""
        values = asdict(self)
        values["start_date"] = self.start_date.isoformat()
        values["end_date"] = self.end_date.isoformat()
        values["environment"] = {
            "app_version": __version__,
            "python": platform.python_version(),
            "faker": package_version("faker"),
        }
        return values

    def config_hash(self) -> str:
        canonical = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return content


def _reject_unknown(section: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"Unknown setting(s) in {where}: {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _parse_override_value(raw: str) -> Any:
    """``--set key=value`` values are YAML scalars: ``500`` → int, ``true`` → bool, ``2025-04-01`` → date."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot read override value {raw!r}: {exc}") from exc


def _apply_override(effective: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = effective
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            raise ConfigError(f"Override {dotted!r}: '{part}' is not a settings section")
        node = node[part]
    if parts[-1] not in node:
        raise ConfigError(f"Override {dotted!r}: unknown setting '{parts[-1]}'")
    node[parts[-1]] = value


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be a whole number, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _date(value: Any, name: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"{name} must be a date (YYYY-MM-DD) or null, got {value!r}")


def _months_before(day: date, months: int) -> date:
    """The day after the same date ``months`` earlier: 24 months ending 2026-11-09 start 2024-11-10."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    last_day = monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day)) + timedelta(days=1)


def load_generation_config(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    preset: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> GenerationConfig:
    """Resolve the effective configuration.

    ``overrides`` use dotted keys relative to the effective settings, e.g.
    ``{"volumes.customers_b2b": 500, "history.start_date": "2025-04-01", "batch_size": 2000}``.
    ``today`` exists for tests; it defaults to the real date in IST.
    """
    raw = _read(config_dir / GENERATION_FILE)
    _reject_unknown(raw, {"defaults", "presets", "setup", "organisation", "master_data", "daily", "opening_balances",
                          "procurement"}, GENERATION_FILE)
    defaults, presets = raw.get("defaults"), raw.get("presets")
    if not isinstance(defaults, dict) or not isinstance(presets, dict) or not presets:
        raise ConfigError(f"{GENERATION_FILE} needs a 'defaults' mapping and at least one preset under 'presets'")
    _reject_unknown(defaults, _DEFAULT_KEYS, "defaults")
    _reject_unknown(defaults.get("history") or {}, _HISTORY_KEYS, "defaults.history")

    chosen = preset or defaults.get("preset")
    if chosen not in presets:
        raise ConfigError(f"Unknown preset {chosen!r}; choose one of: {', '.join(presets)}")
    preset_cfg = presets[chosen]
    if not isinstance(preset_cfg, dict):
        raise ConfigError(f"Preset {chosen!r} must be a mapping")
    _reject_unknown(preset_cfg, _PRESET_KEYS, f"presets.{chosen}")
    _reject_unknown(preset_cfg.get("volumes") or {}, _VOLUME_KEYS, f"presets.{chosen}.volumes")
    missing = sorted(_VOLUME_KEYS - set(preset_cfg.get("volumes") or {}))
    if missing:
        raise ConfigError(f"presets.{chosen}.volumes is missing: {', '.join(missing)}")

    # One flat-ish tree that overrides address with dotted keys.
    effective: dict[str, Any] = {
        "fiscal_year_start_month": defaults.get("fiscal_year_start_month"),
        "batch_size": defaults.get("batch_size"),
        "gst_rate_revision": defaults.get("gst_rate_revision", True),
        "history_months": preset_cfg.get("history_months"),
        "history": {key: (defaults.get("history") or {}).get(key) for key in _HISTORY_KEYS},
        "volumes": copy.deepcopy(preset_cfg["volumes"]),
        "setup": copy.deepcopy(raw.get("setup") or {}),
        "organisation": copy.deepcopy(raw.get("organisation") or {}),
        "master_data": copy.deepcopy(raw.get("master_data") or {}),
        "daily": copy.deepcopy(raw.get("daily") or {}),
        "opening_balances": copy.deepcopy(raw.get("opening_balances") or {}),
        "procurement": copy.deepcopy(raw.get("procurement") or {}),
    }
    for dotted, value in (overrides or {}).items():
        _apply_override(effective, dotted, _parse_override_value(value) if isinstance(value, str) else value)

    fy_month = _int(effective["fiscal_year_start_month"], "fiscal_year_start_month", 1, 12)
    batch_size = _int(effective["batch_size"], "batch_size", 1, 50_000)
    history_months = _int(effective["history_months"], "history_months", 1, 36)
    volumes = Volumes(**{key: _int(effective["volumes"][key], f"volumes.{key}", 1, 10_000_000)
                         for key in sorted(_VOLUME_KEYS)})
    gst_rate_revision = effective["gst_rate_revision"]
    if not isinstance(gst_rate_revision, bool):
        raise ConfigError(f"gst_rate_revision must be true or false, got {gst_rate_revision!r}")
    allow_future = effective["history"]["allow_future"]
    if not isinstance(allow_future, bool):
        raise ConfigError(f"history.allow_future must be true or false, got {allow_future!r}")

    today = today or today_ist()
    end = _date(effective["history"]["end_date"], "history.end_date") or today - timedelta(days=1)
    start = _date(effective["history"]["start_date"], "history.start_date") or _months_before(end, history_months)
    if end >= today and not allow_future:
        raise ConfigError(f"history.end_date {end} is today or later; set history.allow_future to simulate it (D68)")
    if start > end:
        raise ConfigError(f"history.start_date {start} is after history.end_date {end}")
    if (end - start).days + 1 > MAX_HISTORY_DAYS:
        raise ConfigError(f"History of {(end - start).days + 1} days exceeds the 3-year maximum (1G §3)")

    setup = _parse_setup(effective["setup"])
    organisation = _parse_organisation(effective["organisation"])
    master_data = _parse_master_data(effective["master_data"])
    daily = _parse_daily(effective["daily"])
    opening = _parse_opening(effective["opening_balances"])
    procurement = _parse_procurement(effective["procurement"])
    return GenerationConfig(
        procurement=procurement,
        opening_balances=opening,
        daily=daily,
        master_data=master_data,
        setup=setup,
        organisation=organisation,
        preset=chosen,
        start_date=start,
        end_date=end,
        allow_future=allow_future,
        fiscal_year_start_month=fy_month,
        batch_size=batch_size,
        volumes=volumes,
        gst_rate_revision=gst_rate_revision,
    )


def _pair(value: Any, name: str, low: int, high: int) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ConfigError(f"{name} must be [min, max]")
    a, b = _int(value[0], f"{name} min", low, high), _int(value[1], f"{name} max", low, high)
    if a > b:
        raise ConfigError(f"{name}: min {a} is above max {b}")
    return a, b


def _parse_setup(raw: dict[str, Any]) -> Setup:
    _reject_unknown(raw, _SETUP_KEYS, "setup")
    missing = sorted(_SETUP_KEYS - set(raw))
    if missing:
        raise ConfigError(f"setup is missing: {', '.join(missing)}")
    dcs = raw["distribution_centres"]
    stores = raw["stores"] or []
    if not isinstance(dcs, list) or not dcs or not all(isinstance(c, str) for c in dcs):
        raise ConfigError("setup.distribution_centres must list at least one city")
    if not isinstance(stores, list) or not all(isinstance(c, str) for c in stores):
        raise ConfigError("setup.stores must be a list of cities")
    if len(set(dcs)) != len(dcs):
        raise ConfigError("setup.distribution_centres lists a city twice")
    for city in stores:
        if city not in dcs:
            raise ConfigError(f"setup.stores: {city} has no distribution centre to replenish it (1B §4.3)")
    if not isinstance(raw["hq_city"], str):
        raise ConfigError("setup.hq_city must be a city name")
    return Setup(raw["hq_city"], tuple(dcs), tuple(stores), _pair(raw["company_age_years"], "setup.company_age_years", 1, 80),
                 _int(raw["cash_credit_limit"], "setup.cash_credit_limit", 1, 10**12))


def _parse_organisation(raw: dict[str, Any]) -> Organisation:
    _reject_unknown(raw, _ORG_KEYS, "organisation")
    missing = sorted(_ORG_KEYS - set(raw))
    if missing:
        raise ConfigError(f"organisation is missing: {', '.join(missing)}")
    levels_raw = raw["job_levels"] or {}
    if set(levels_raw) != set(JOB_LEVELS):
        raise ConfigError(f"organisation.job_levels must define exactly {', '.join(JOB_LEVELS)}")
    levels = {}
    for level in JOB_LEVELS:
        entry = levels_raw[level] or {}
        _reject_unknown(entry, {"salary", "approval_limit"}, f"organisation.job_levels.{level}")
        levels[level] = JobLevel(_pair(entry.get("salary"), f"job_levels.{level}.salary", 1, 10**8),
                                 _int(entry.get("approval_limit"), f"job_levels.{level}.approval_limit", 0, 10**12))
    share = raw["warehouse_staff_share"]
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share < 1:
        raise ConfigError("organisation.warehouse_staff_share must be between 0 and 1")
    departments = []
    for code, entry in (raw["departments"] or {}).items():
        entry = entry or {}
        _reject_unknown(entry, {"weight", "head", "manager", "staff"}, f"organisation.departments.{code}")
        weight = entry.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise ConfigError(f"organisation.departments.{code}.weight must be positive")
        titles = [entry.get(k) for k in ("head", "manager", "staff")]
        if not all(isinstance(t, str) and t for t in titles):
            raise ConfigError(f"organisation.departments.{code} needs head, manager and staff titles")
        departments.append(Department(code, float(weight), *titles))
    if not departments:
        raise ConfigError("organisation.departments must list the central departments")
    titles = raw["warehouse_titles"] or {}
    if set(titles) != {"WHO", "store"} or set(titles["WHO"]) != {"head", "dc_manager", "lead", "staff"} \
            or set(titles["store"]) != {"manager", "staff"}:
        raise ConfigError("organisation.warehouse_titles needs WHO (head, dc_manager, lead, staff) and store (manager, staff)")
    if not isinstance(raw["chief_executive"], str):
        raise ConfigError("organisation.chief_executive must be a job title")
    return Organisation(levels, float(share), tuple(departments), titles, raw["chief_executive"])


def _share(value: Any, name: str, high: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= high:
        raise ConfigError(f"{name} must be a number between 0 and {high}, got {value!r}")
    return float(value)


def _mix(value: Any, name: str, allowed: Iterable[str]) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{name} must be a mapping of code: share")
    _reject_unknown(value, set(allowed), name)
    mix = {key: _share(share, f"{name}.{key}") for key, share in value.items()}
    if abs(sum(mix.values()) - 1) > 1e-6:
        raise ConfigError(f"{name}: shares add up to {sum(mix.values()):.4f}, not 1")
    return mix


def _parse_master_data(raw: dict[str, Any]) -> MasterDataSettings:
    _reject_unknown(raw, set(_MASTER_SCHEMA), "master_data")
    for section, keys in _MASTER_SCHEMA.items():
        entry = raw.get(section)
        if not isinstance(entry, dict):
            raise ConfigError(f"master_data.{section} is missing")
        _reject_unknown(entry, keys, f"master_data.{section}")
        missing = sorted(keys - set(entry))
        if missing:
            raise ConfigError(f"master_data.{section} is missing: {', '.join(missing)}")
    c, s_, k = raw["customers"], raw["suppliers"], raw["campaigns"]
    limits = c["credit_limits"] or {}
    suffixes = c["name_suffixes"] or {}
    if set(limits) != set(B2B_SEGMENTS) or set(suffixes) != set(B2B_SEGMENTS):
        raise ConfigError(f"master_data.customers credit_limits and name_suffixes need every segment: {', '.join(B2B_SEGMENTS)}")
    for segment, names in suffixes.items():
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"master_data.customers.name_suffixes.{segment} must list names")
    attribution = c["campaign_attribution"] or {}
    _reject_unknown(attribution, {"B2B", "B2C"}, "master_data.customers.campaign_attribution")
    mean_years = c["acquisition_mean_years"]
    if isinstance(mean_years, bool) or not isinstance(mean_years, (int, float)) or not 0 < mean_years <= 20:
        raise ConfigError("master_data.customers.acquisition_mean_years must be between 0 and 20")
    region_weight = c["warehouse_region_weight"]
    if isinstance(region_weight, bool) or not isinstance(region_weight, (int, float)) or not 1 <= region_weight <= 20:
        raise ConfigError("master_data.customers.warehouse_region_weight must be between 1 and 20")
    return MasterDataSettings(
        b2b_segments=_mix(c["b2b_segments"], "master_data.customers.b2b_segments", B2B_SEGMENTS),
        b2b_registration=_mix(c["b2b_registration"], "master_data.customers.b2b_registration",
                              ("REGISTERED", "COMPOSITION", "UNREGISTERED")),
        credit_limits={seg: _pair(v, f"master_data.customers.credit_limits.{seg}", 1, 10**10) for seg, v in limits.items()},
        customer_name_suffixes={seg: tuple(v) for seg, v in suffixes.items()},
        extra_shipping_address_share=_share(c["extra_shipping_address_share"], "master_data.customers.extra_shipping_address_share"),
        campaign_attribution={kind: _share(attribution.get(kind, 0), f"master_data.customers.campaign_attribution.{kind}")
                              for kind in ("B2B", "B2C")},
        acquisition_mean_years=float(mean_years), warehouse_region_weight=float(region_weight),
        supplier_registration=_mix(s_["goods_registration"], "master_data.suppliers.goods_registration",
                                   ("REGISTERED", "COMPOSITION", "UNREGISTERED")),
        msme_share=_share(s_["msme_share"], "master_data.suppliers.msme_share"),
        dispatch_address_share=_share(s_["dispatch_address_share"], "master_data.suppliers.dispatch_address_share"),
        suppliers_per_product=_pair(s_["per_product"], "master_data.suppliers.per_product", 2, 20),
        campaign_history_years=_int(k["history_years"], "master_data.campaigns.history_years", 0, 5),
        campaign_cancelled_share=_share(k["cancelled_share"], "master_data.campaigns.cancelled_share", 0.5),
        campaign_budget=_pair(k["budget"], "master_data.campaigns.budget", 1, 10**10),
    )


def _parse_daily(raw: dict[str, Any]) -> DailySettings:
    _reject_unknown(raw, _DAILY_KEYS, "daily")
    missing = sorted(_DAILY_KEYS - set(raw))
    if missing:
        raise ConfigError(f"daily is missing: {', '.join(missing)}")
    off = raw["weekly_off"] or []
    if not isinstance(off, list) or any(d not in WEEKDAYS for d in off) or len(off) > 2:
        raise ConfigError(f"daily.weekly_off must list up to two of {', '.join(WEEKDAYS)}")
    holidays = []
    for item in raw["national_holidays"] or []:
        try:
            month, day = (int(part) for part in str(item).split("-"))
            date(2001, month, day)
        except ValueError:
            raise ConfigError(f"daily.national_holidays: {item!r} is not an MM-DD date") from None
        holidays.append((month, day))
    return DailySettings(
        weekly_off=tuple(WEEKDAYS.index(d) for d in off), national_holidays=tuple(holidays),
        period_close_day=_int(raw["period_close_day"], "daily.period_close_day", 1, 28),
        progress_every_days=_int(raw["progress_every_days"], "daily.progress_every_days", 1, 366),
        new_products_per_year=_share(raw["new_products_per_year"], "daily.new_products_per_year"),
        discontinued_per_year=_share(raw["discontinued_per_year"], "daily.discontinued_per_year", 0.5),
        campaign_lead_days=_int(raw["campaign_lead_days"], "daily.campaign_lead_days", 1, 120),
    )


def _parse_opening(raw: dict[str, Any]) -> OpeningBalances:
    _reject_unknown(raw, _OPENING_KEYS, "opening_balances")
    missing = sorted(_OPENING_KEYS - set(raw))
    if missing:
        raise ConfigError(f"opening_balances is missing: {', '.join(missing)}")

    def amounts(value: Any, name: str, allowed: set[str] | None = None) -> dict[str, int]:
        if not isinstance(value, dict) or not value:
            raise ConfigError(f"opening_balances.{name} must be a mapping of name: amount")
        if allowed is not None:
            _reject_unknown(value, allowed, f"opening_balances.{name}")
        return {str(k): _int(v, f"opening_balances.{name}.{k}", 0, 10**12) for k, v in value.items()}

    fixed = amounts(raw["fixed_assets"], "fixed_assets")
    for code in fixed:
        if not code.isdigit() or len(code) != 5 or code[0] != "1":
            raise ConfigError(f"opening_balances.fixed_assets: {code!r} must be a 5-digit asset account code")
    return OpeningBalances(
        share_capital=_int(raw["share_capital"], "opening_balances.share_capital", 1, 10**12),
        bank_balances=amounts(raw["bank_balances"], "bank_balances", {"COLLECTIONS", "PAYMENTS"}),
        cash_credit_drawn=_int(raw["cash_credit_drawn"], "opening_balances.cash_credit_drawn", 0, 10**12),
        petty_cash=amounts(raw["petty_cash"], "petty_cash", {"DISTRIBUTION_CENTRE", "RETAIL_STORE"}),
        fixed_assets=fixed,
        accumulated_depreciation_share=_share(raw["accumulated_depreciation_share"],
                                              "opening_balances.accumulated_depreciation_share", 0.95),
        store_assortment_share=_share(raw["store_assortment_share"], "opening_balances.store_assortment_share"),
    )


def _float_pair(value: Any, name: str, low: float, high: float) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                                              for v in value):
        raise ConfigError(f"{name} must be [min, max] numbers")
    a, b = float(value[0]), float(value[1])
    if not low <= a <= b <= high:
        raise ConfigError(f"{name} must satisfy {low} <= min <= max <= {high}, got {value}")
    return a, b


def _parse_procurement(raw: dict[str, Any]) -> ProcurementSettings:
    _reject_unknown(raw, _PROCUREMENT_KEYS, "procurement")
    missing = sorted(_PROCUREMENT_KEYS - set(raw))
    if missing:
        raise ConfigError(f"procurement is missing: {', '.join(missing)}")
    weights = _mix(raw["score_weights"], "procurement.score_weights", ("price", "lead_time", "rating"))
    ceiling = raw["award_price_ceiling"]
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 1 <= ceiling <= 3:
        raise ConfigError("procurement.award_price_ceiling must be between 1 and 3")
    return ProcurementSettings(
        direct_reorder_max_value=_int(raw["direct_reorder_max_value"], "procurement.direct_reorder_max_value", 0, 10**10),
        rfq_max_lines=_int(raw["rfq_max_lines"], "procurement.rfq_max_lines", 1, 50),
        rfq_suppliers=_pair(raw["rfq_suppliers"], "procurement.rfq_suppliers", 1, 20),
        rfq_response_days=_pair(raw["rfq_response_days"], "procurement.rfq_response_days", 1, 60),
        response_mix=_mix(raw["response_mix"], "procurement.response_mix", ("QUOTED", "DECLINED", "NO_RESPONSE")),
        quote_price_factor=_float_pair(raw["quote_price_factor"], "procurement.quote_price_factor", 0.5, 2.0),
        quote_lead_factor=_float_pair(raw["quote_lead_factor"], "procurement.quote_lead_factor", 0.1, 3.0),
        quote_validity_days=_pair(raw["quote_validity_days"], "procurement.quote_validity_days", 1, 180),
        evaluation_days=_pair(raw["evaluation_days"], "procurement.evaluation_days", 1, 30),
        award_price_ceiling=float(ceiling), score_weights=weights,
        approval_days=_pair(raw["approval_days"], "procurement.approval_days", 0, 30),
        po_cancel_share=_share(raw["po_cancel_share"], "procurement.po_cancel_share", 0.5),
    )
