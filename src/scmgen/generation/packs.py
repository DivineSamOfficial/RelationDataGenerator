"""Industry packs (D11, 1A §8.3): load ``config/industries/<code>.yaml`` and check it.

A pack is pure data: catalogue tree, invented brands, product templates (HSN, GST before/after the
22 Sep 2025 revision, MRP bands per brand tier, physical ranges, expiry, warranty), customer and
channel mixes, seasonality and sale events, return rates, supplier profile and pricing ratios.
``electronics.yaml`` documents the format field by field.

The checks guarantee that products generated from a pack can satisfy the database and the validation
rules: e.g. a price band exists for every brand tier that sells in a category, an HSN code always
carries one GST rate (V-MD-19, also across packs), and a rate never rises at the revision, so list
price + GST stays within MRP (V-MD-18). Every problem is reported at once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.generation.seed import SeedData

PACK_DIR = "industries"
GST_SLABS = frozenset(Decimal(v) for v in ("0", "0.25", "3", "5", "12", "18", "28", "40"))
BRAND_TIERS = ("ECONOMY", "MID", "PREMIUM")
CUSTOMER_TYPES = ("B2B", "B2C")
SHARE_TOLERANCE = Decimal("0.0001")

_TOP_KEYS = {"code", "name", "business_unit", "customers", "marketplace_commission_percent", "seasonality",
             "events", "returns", "suppliers", "pricing", "brands", "name_series", "categories"}
_LEAF_KEYS = {"code", "name", "share", "hsn", "gst", "uom", "mrp", "weight_kg", "volume_litres", "warranty_months",
              "shelf_life_days", "nouns", "variants", "colours", "seasonality", "returnable", "return_window_days"}
_BRANCH_KEYS = {"code", "name", "return_window_days", "children"}
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,19}$")
_HSN = re.compile(r"^[0-9]{4}([0-9]{2}){0,2}$")


class PackError(ConfigError):
    """An industry pack is missing or inconsistent; the message lists every problem found."""


@dataclass(frozen=True)
class Range:
    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class Variant:
    label: str
    price_factor: Decimal


@dataclass(frozen=True)
class SaleEvent:
    name: str
    month: int
    day: int
    days: int
    uplift: Decimal
    channels: tuple[str, ...]


@dataclass(frozen=True)
class Brand:
    code: str
    name: str
    tier: str
    private_label: bool
    categories: tuple[str, ...]       # level-1 category codes


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    parent_code: str | None
    level: int
    return_window_days: int
    is_leaf: bool
    # Product template (leaves only)
    share: Decimal | None = None
    hsn: str | None = None
    gst_before: Decimal | None = None
    gst_after: Decimal | None = None
    uom: str | None = None
    mrp: Mapping[str, Range] | None = None
    weight_kg: Range | None = None
    volume_litres: Range | None = None
    warranty_months: int | None = None
    shelf_life_days: int | None = None
    returnable: bool = True
    nouns: tuple[str, ...] = ()
    variants: tuple[Variant, ...] = ()
    colours: tuple[str, ...] = ()
    seasonality: tuple[Decimal, ...] | None = None

    @property
    def has_expiry(self) -> bool:
        return self.shelf_life_days is not None


@dataclass(frozen=True)
class IndustryPack:
    code: str
    name: str
    business_unit_code: str
    business_unit_name: str
    b2b_sales_share: Decimal
    credit_terms: Mapping[str, Mapping[str, Decimal]]          # per customer type that buys (B2B / B2C)
    sales_channels: Mapping[str, Mapping[str, Decimal]]
    marketing_channels: Mapping[str, Mapping[str, Decimal]]
    marketplace_commission_percent: Decimal
    seasonality: tuple[Decimal, ...]
    events: tuple[SaleEvent, ...]
    returns: Mapping[str, Decimal]
    supplier_hubs: tuple[str, ...]
    supplier_name_suffixes: tuple[str, ...]
    supplier_cost_drift: Range
    supplier_lead_time_days: tuple[int, int]
    list_price_ratio: Range
    gross_margin: Range
    brands: tuple[Brand, ...]
    name_series: tuple[str, ...]
    categories: tuple[Category, ...]          # parents before children

    @property
    def leaves(self) -> tuple[Category, ...]:
        return tuple(c for c in self.categories if c.is_leaf)

    def top_category(self, category: Category) -> Category:
        by_code = {c.code: c for c in self.categories}
        while category.parent_code is not None:
            category = by_code[category.parent_code]
        return category

    def brands_for(self, category: Category) -> tuple[Brand, ...]:
        top = self.top_category(category).code
        return tuple(b for b in self.brands if top in b.categories)


def available_packs(config_dir: Path = DEFAULT_CONFIG_DIR) -> list[str]:
    """Codes of the packs shipped in ``config/industries``."""
    return sorted(p.stem for p in (config_dir / PACK_DIR).glob("*.yaml"))


# ------------------------------------------------------------------------------------------ parsing


class _Checker:
    """Collects problems while converting raw YAML values."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.problems: list[str] = []

    def fail(self, message: str) -> None:
        self.problems.append(f"{self.source}: {message}")

    def decimal(self, value: Any, where: str, low: Decimal | int | None = None, high: Decimal | int | None = None,
                low_inclusive: bool = True) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            self.fail(f"{where} must be a number, got {value!r}")
            return Decimal(0)
        number = Decimal(str(value))
        if low is not None and (number < low or (not low_inclusive and number == low)):
            self.fail(f"{where} must be {'>=' if low_inclusive else '>'} {low}, got {number}")
        if high is not None and number > high:
            self.fail(f"{where} must be <= {high}, got {number}")
        return number

    def integer(self, value: Any, where: str, low: int, high: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            self.fail(f"{where} must be a whole number {low}-{high}, got {value!r}")
            return low
        return value

    def range(self, value: Any, where: str, low: Decimal | int = 0, high: Decimal | int | None = None,
              positive: bool = True) -> Range:
        if not isinstance(value, list) or len(value) != 2:
            self.fail(f"{where} must be [min, max], got {value!r}")
            return Range(Decimal(1), Decimal(1))
        a = self.decimal(value[0], f"{where} min", low, high, low_inclusive=not positive)
        b = self.decimal(value[1], f"{where} max", low, high, low_inclusive=not positive)
        if a > b:
            self.fail(f"{where}: min {a} is above max {b}")
        return Range(a, b)

    def shares(self, value: Any, where: str, allowed: Iterable[str]) -> dict[str, Decimal]:
        if not isinstance(value, dict) or not value:
            self.fail(f"{where} must be a mapping of code: share")
            return {}
        allowed = set(allowed)
        result = {}
        for key, share in value.items():
            if key not in allowed:
                self.fail(f"{where}: {key!r} is not allowed here (allowed: {', '.join(sorted(allowed))})")
            result[key] = self.decimal(share, f"{where}.{key}", 0, 1, low_inclusive=False)
        if abs(sum(result.values(), Decimal(0)) - 1) > SHARE_TOLERANCE:
            self.fail(f"{where}: shares add up to {sum(result.values(), Decimal(0))}, not 1")
        return result

    def curve(self, value: Any, where: str) -> tuple[Decimal, ...]:
        if not isinstance(value, list) or len(value) != 12:
            self.fail(f"{where} must list 12 monthly factors (Jan-Dec)")
            return tuple(Decimal(1) for _ in range(12))
        factors = tuple(self.decimal(v, f"{where}[{i + 1}]", 0, 5, low_inclusive=False) for i, v in enumerate(value))
        mean = sum(factors, Decimal(0)) / 12
        if abs(mean - 1) > Decimal("0.02"):
            self.fail(f"{where}: monthly factors must average 1.0 (got {mean:.3f}), so volumes stay as configured")
        return factors

    def known(self, value: Mapping[str, Any], allowed: set[str], where: str) -> None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            self.fail(f"{where}: unknown field(s) {', '.join(unknown)}")


def _categories(check: _Checker, raw: Any, seed: SeedData) -> tuple[Category, ...]:
    uoms = {row["uom_code"] for row in seed.units_of_measure}
    result: list[Category] = []

    def walk(nodes: Any, parent: Category | None) -> None:
        if not isinstance(nodes, list) or not nodes:
            check.fail(f"{parent.code if parent else 'categories'}: needs a non-empty list of categories")
            return
        for node in nodes:
            if not isinstance(node, dict):
                check.fail("categories: every entry must be a mapping")
                continue
            code = str(node.get("code"))
            where = f"category {code}"
            if not _CODE.match(code):
                check.fail(f"{where}: code must match {_CODE.pattern}")
            level = 1 if parent is None else parent.level + 1
            if level > 3:
                check.fail(f"{where}: categories are at most 3 levels deep")
            window = node.get("return_window_days", parent.return_window_days if parent else None)
            window = check.integer(window, f"{where}.return_window_days", 0, 365)
            if "children" in node:
                check.known(node, _BRANCH_KEYS, where)
                category = Category(code, str(node.get("name")), parent.code if parent else None, level, window, False)
                result.append(category)
                walk(node["children"], category)
                continue
            check.known(node, _LEAF_KEYS, where)
            if parent is None:
                check.fail(f"{where}: a product category needs a parent (level-1 entries group leaves)")
            hsn = str(node.get("hsn"))
            if not _HSN.match(hsn):
                check.fail(f"{where}: HSN {hsn!r} must have 4, 6 or 8 digits")
            gst = node.get("gst") if isinstance(node.get("gst"), dict) else {}
            before = check.decimal(gst.get("before"), f"{where}.gst.before", 0, 50)
            after = check.decimal(gst.get("after"), f"{where}.gst.after", 0, 50)
            for rate in (before, after):
                if rate not in GST_SLABS:
                    check.fail(f"{where}: GST rate {rate} is not a GST slab ({', '.join(str(s) for s in sorted(GST_SLABS))})")
            if after > before:
                check.fail(f"{where}: GST rises {before} -> {after} at the revision; list price + GST could then exceed "
                           "MRP (V-MD-18)")
            if node.get("uom") not in uoms:
                check.fail(f"{where}: unknown unit of measure {node.get('uom')!r}")
            mrp_raw = node.get("mrp") if isinstance(node.get("mrp"), dict) else {}
            if not mrp_raw:
                check.fail(f"{where}: needs MRP bands per brand tier")
            mrp = {}
            for tier, band in mrp_raw.items():
                if tier not in BRAND_TIERS:
                    check.fail(f"{where}: unknown brand tier {tier!r} in mrp")
                mrp[tier] = check.range(band, f"{where}.mrp.{tier}")
            shelf_life = node.get("shelf_life_days")
            variants = []
            for variant in node.get("variants") or []:
                if not isinstance(variant, dict) or not variant.get("label"):
                    check.fail(f"{where}: every variant needs a label and a price_factor")
                    continue
                variants.append(Variant(str(variant["label"]),
                                        check.decimal(variant.get("price_factor"), f"{where} variant price_factor",
                                                      0, 20, low_inclusive=False)))
            nouns = tuple(str(n) for n in node.get("nouns") or [])
            if not nouns:
                check.fail(f"{where}: needs product nouns for names")
            returnable = node.get("returnable", window > 0)
            if not isinstance(returnable, bool):
                check.fail(f"{where}: returnable must be true or false")
            result.append(Category(
                code=code, name=str(node.get("name")), parent_code=parent.code if parent else None, level=level,
                return_window_days=window, is_leaf=True,
                share=check.decimal(node.get("share"), f"{where}.share", 0, 1000, low_inclusive=False),
                hsn=hsn, gst_before=before, gst_after=after, uom=node.get("uom"), mrp=mrp,
                weight_kg=check.range(node.get("weight_kg"), f"{where}.weight_kg"),
                volume_litres=check.range(node.get("volume_litres"), f"{where}.volume_litres"),
                warranty_months=None if node.get("warranty_months") is None
                else check.integer(node.get("warranty_months"), f"{where}.warranty_months", 1, 255),
                shelf_life_days=None if shelf_life is None
                else check.integer(shelf_life, f"{where}.shelf_life_days", 1, 65535),
                returnable=bool(returnable), nouns=nouns, variants=tuple(variants),
                colours=tuple(str(c) for c in node.get("colours") or []),
                seasonality=None if node.get("seasonality") is None
                else check.curve(node.get("seasonality"), f"{where}.seasonality"),
            ))

    walk(raw, None)
    codes = [c.code for c in result]
    for code in sorted({c for c in codes if codes.count(c) > 1}):
        check.fail(f"category code {code} is used twice")
    return tuple(result)


def load_pack(code: str, seed: SeedData, config_dir: Path = DEFAULT_CONFIG_DIR) -> IndustryPack:
    """Load and check one pack; raises ``PackError`` listing every problem."""
    path = config_dir / PACK_DIR / f"{code}.yaml"
    if not path.exists():
        raise PackError(f"No industry pack '{code}'. Available: {', '.join(available_packs(config_dir)) or 'none'}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PackError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackError(f"{path} must contain a mapping at the top level")
    check = _Checker(f"{code}.yaml")
    check.known(raw, _TOP_KEYS, "pack")
    for key in sorted(_TOP_KEYS - set(raw)):
        check.fail(f"missing section '{key}'")
    if raw.get("code") != code:
        check.fail(f"code {raw.get('code')!r} must equal the file name {code!r}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", code):
        check.fail("pack code must be lower-case letters, digits and underscores (business_units.industry_pack_code)")
    bu = raw.get("business_unit") if isinstance(raw.get("business_unit"), dict) else {}
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", str(bu.get("code"))):
        check.fail(f"business_unit.code {bu.get('code')!r} must be 2-10 capital letters/digits")

    terms = {row["term_code"]: row for row in seed.payment_terms}
    sales = {row["channel_code"]: row for row in seed.sales_channels}
    marketing = {row["channel_code"]: row for row in seed.marketing_channels}
    customers = raw.get("customers") if isinstance(raw.get("customers"), dict) else {}
    b2b_share = check.decimal(customers.get("b2b_sales_share"), "customers.b2b_sales_share", 0, 1)
    # A B2B-only pack (share 1) has no consumer mixes, a B2C-only pack (share 0) no business ones.
    kinds = [k for k, used in (("B2B", b2b_share > 0), ("B2C", b2b_share < 1)) if used]
    for section in ("credit_terms", "sales_channels", "marketing_channels"):
        given = set((customers.get(section) or {}).keys())
        for kind in sorted(given - set(kinds)):
            check.fail(f"{section}.{kind} is given but b2b_sales_share = {b2b_share} means no {kind} sales")
    credit, channels, media = {}, {}, {}
    for kind in kinds:
        credit[kind] = check.shares((customers.get("credit_terms") or {}).get(kind), f"credit_terms.{kind}", terms)
        channels[kind] = check.shares((customers.get("sales_channels") or {}).get(kind), f"sales_channels.{kind}",
                                      [c for c, row in sales.items() if row["allowed_customer_type"] in (kind, "BOTH")])
        media[kind] = check.shares((customers.get("marketing_channels") or {}).get(kind),
                                   f"marketing_channels.{kind}", marketing)
    for term in credit.get("B2C", {}):
        if term in terms and terms[term]["due_days"] != 0:
            check.fail(f"credit_terms.B2C: consumers get no credit, {term} has {terms[term]['due_days']} days")

    events = []
    for index, event in enumerate(raw.get("events") or [], 1):
        where = f"events[{index}]"
        if not isinstance(event, dict):
            check.fail(f"{where} must be a mapping")
            continue
        month = check.integer(event.get("month"), f"{where}.month", 1, 12)
        day = check.integer(event.get("day"), f"{where}.day", 1, 31)
        try:
            date(2001, month, day)                     # a non-leap year: no 29 February events
        except ValueError:
            check.fail(f"{where}: {day}/{month} is not a date in every year")
        for channel in event.get("channels") or []:
            if channel not in marketing or not marketing[channel]["supports_campaigns"]:
                check.fail(f"{where}: channel {channel!r} can't carry campaigns")
        name = str(event.get("name") or "")
        if not 0 < len(name) <= 150:
            check.fail(f"{where}: name must be 1-150 characters (marketing_campaigns.campaign_name)")
        events.append(SaleEvent(name, month, day, check.integer(event.get("days"), f"{where}.days", 1, 60),
                                check.decimal(event.get("uplift"), f"{where}.uplift", 1, 3),
                                tuple(event.get("channels") or [])))
    if not events:
        check.fail("events: at least one sale event per year (campaigns, 1B §4.7)")

    returns_raw = raw.get("returns") if isinstance(raw.get("returns"), dict) else {}
    # Rates are shares of lines / units (capped at 50%); defective_share is a share *of returns* (up to 100%).
    returns = {key: check.decimal(returns_raw.get(key), f"returns.{key}", 0, 1 if key == "defective_share" else Decimal("0.5"))
               for key in ("b2c_return_rate", "b2b_return_rate", "defective_share", "transit_damage_rate")}
    check.known(returns_raw, set(returns), "returns")

    suppliers = raw.get("suppliers") if isinstance(raw.get("suppliers"), dict) else {}
    city_names = {row["city"] for row in seed.cities}
    hubs = tuple(suppliers.get("hubs") or [])
    for hub in hubs:
        if hub not in city_names:
            check.fail(f"suppliers.hubs: {hub!r} is not in config/seed/cities.yaml")
    if not hubs or not suppliers.get("name_suffixes"):
        check.fail("suppliers needs hubs and name_suffixes")
    drift = check.range(suppliers.get("monthly_cost_drift"), "suppliers.monthly_cost_drift", 0, Decimal("0.05"),
                        positive=False)
    lead = suppliers.get("lead_time_days") or [0, 0]
    lead_low = check.integer(lead[0] if len(lead) == 2 else None, "suppliers.lead_time_days min", 1, 120)
    lead_high = check.integer(lead[1] if len(lead) == 2 else None, "suppliers.lead_time_days max", lead_low, 120)

    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    ratio = check.range(pricing.get("list_price_ratio"), "pricing.list_price_ratio", 0, 1)
    margin = check.range(pricing.get("gross_margin"), "pricing.gross_margin", 0, Decimal("0.9"))

    categories = _categories(check, raw.get("categories"), seed)
    top_codes = {c.code for c in categories if c.level == 1}
    brands = []
    for entry in raw.get("brands") or []:
        brand_code = str(entry.get("code"))
        if not _CODE.match(brand_code):
            check.fail(f"brand {brand_code}: code must match {_CODE.pattern}")
        if entry.get("tier") not in BRAND_TIERS:
            check.fail(f"brand {brand_code}: tier must be one of {', '.join(BRAND_TIERS)}")
        sells = tuple(entry.get("categories") or [])
        for category_code in sells:
            if category_code not in top_codes:
                check.fail(f"brand {brand_code}: {category_code!r} is not a level-1 category of this pack")
        if not sells:
            check.fail(f"brand {brand_code}: lists no categories")
        brands.append(Brand(brand_code, str(entry.get("name")), str(entry.get("tier")),
                            bool(entry.get("private_label", False)), sells))
    for attribute in ("code", "name"):
        values = [getattr(b, attribute) for b in brands]
        for value in sorted({v for v in values if values.count(v) > 1}):
            check.fail(f"brand {attribute} {value!r} is used twice")

    pack = IndustryPack(
        code=code, name=str(raw.get("name")), business_unit_code=str(bu.get("code")),
        business_unit_name=str(bu.get("name")),
        b2b_sales_share=b2b_share,
        credit_terms=credit, sales_channels=channels, marketing_channels=media,
        marketplace_commission_percent=check.decimal(raw.get("marketplace_commission_percent"),
                                                     "marketplace_commission_percent", 0, 100),
        seasonality=check.curve(raw.get("seasonality"), "seasonality"), events=tuple(events), returns=returns,
        supplier_hubs=hubs, supplier_name_suffixes=tuple(suppliers.get("name_suffixes") or []),
        supplier_cost_drift=drift, supplier_lead_time_days=(lead_low, lead_high),
        list_price_ratio=ratio, gross_margin=margin, brands=tuple(brands),
        name_series=tuple(raw.get("name_series") or []), categories=categories)

    for leaf in pack.leaves:
        selling = pack.brands_for(leaf)
        if not selling:
            check.fail(f"category {leaf.code}: no brand sells in {pack.top_category(leaf).code}")
        for brand in selling:
            if leaf.mrp is not None and brand.tier not in leaf.mrp:
                check.fail(f"category {leaf.code}: no {brand.tier} MRP band for brand {brand.code}")
    _check_hsn_rates(check, [pack])
    if not pack.name_series:
        check.fail("name_series: needs words for product names")
    if check.problems:
        raise PackError("Industry pack problems:\n  - " + "\n  - ".join(check.problems))
    return pack


def _check_hsn_rates(check: _Checker, packs: Sequence[IndustryPack]) -> None:
    """V-MD-19: one HSN code, one GST rate on any date (before and after the revision)."""
    seen: dict[str, tuple[Decimal | None, Decimal | None, str]] = {}
    for pack in packs:
        for leaf in pack.leaves:
            assert leaf.hsn is not None
            rates = (leaf.gst_before, leaf.gst_after)
            if leaf.hsn in seen and seen[leaf.hsn][:2] != rates:
                other = seen[leaf.hsn][2]
                check.fail(f"HSN {leaf.hsn}: {leaf.code} has GST {rates[0]}/{rates[1]} but {other} has "
                           f"{seen[leaf.hsn][0]}/{seen[leaf.hsn][1]} (V-MD-19)")
            seen.setdefault(leaf.hsn, (*rates, f"{pack.code}:{leaf.code}"))


def load_packs(codes: Sequence[str], seed: SeedData, config_dir: Path = DEFAULT_CONFIG_DIR) -> list[IndustryPack]:
    """Load the workspace's packs and check that they can coexist (one business unit each)."""
    packs = [load_pack(code, seed, config_dir) for code in codes]
    check = _Checker("packs " + "+".join(codes))
    for attribute, label in [("business_unit_code", "business unit code")]:
        values = [getattr(p, attribute) for p in packs]
        for value in sorted({v for v in values if values.count(v) > 1}):
            check.fail(f"{label} {value} is used by more than one pack")
    for label, values in [("category code", [c.code for p in packs for c in p.categories]),
                          ("brand code", [b.code for p in packs for b in p.brands]),
                          ("brand name", [b.name for p in packs for b in p.brands])]:
        for value in sorted({v for v in values if values.count(v) > 1}):
            check.fail(f"{label} {value!r} appears in more than one pack")
    _check_hsn_rates(check, packs)
    if check.problems:
        raise PackError("Industry packs don't fit together:\n  - " + "\n  - ".join(check.problems))
    return packs
