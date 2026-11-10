"""Catalogue master data from the industry packs (1B §4.6; step 3.5b): brands, category trees,
products and their GST rate history.

Products are generated as *styles*: brand + name series + noun (e.g. "Voltrix Pro Wireless Earbuds"),
one SKU per variant × colour, sharing a style code (V-MD-17). MRP comes from the brand tier's band with
Indian price endings; the list price (ex-GST trade price) is MRP without GST × the pack's trade ratio,
rounded down, so list price + GST never exceeds MRP (V-MD-18). Every product here launches before the
history starts; launches during the history are the daily engine's job (1B "new-launch curve").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from scmgen.core.money import quantity, rate, sql_divide
from scmgen.generation.master import codes
from scmgen.generation.master.context import MasterContext
from scmgen.generation.packs import Category, IndustryPack

CENT = Decimal("0.01")


def charm_price(value: Decimal) -> Decimal:
    """Indian retail price endings: ₹49 / ₹499 / ₹1,299 / ₹24,999."""
    rupees = int(value)
    if rupees < 100:
        return Decimal(max(9, rupees // 10 * 10 + 9))
    if rupees < 1000:
        return Decimal(rupees // 10 * 10 + 9)
    if rupees < 20000:
        return Decimal(rupees // 100 * 100 + 99)
    return Decimal(rupees // 1000 * 1000 + 999)


def _uniform(rng, low: Decimal, high: Decimal, places: int) -> Decimal:
    scale = 10 ** places
    return Decimal(rng.randint(int(low * scale), int(high * scale))) / scale


def _quotas(total: int, weights: list[Decimal]) -> list[int]:
    """Split ``total`` by weights with the largest-remainder method (exact total, deterministic)."""
    whole = sum(weights)
    raw = [total * w / whole for w in weights]
    quotas = [int(x) for x in raw]
    for index in sorted(range(len(raw)), key=lambda i: raw[i] - quotas[i], reverse=True)[:total - sum(quotas)]:
        quotas[index] += 1
    return quotas


def build_catalogue(ctx: MasterContext) -> None:
    products_per_pack = _quotas(ctx.config.volumes.products, [Decimal(1)] * len(ctx.packs))
    for pack, count in zip(ctx.packs, products_per_pack):
        _brands(ctx, pack)
        _categories(ctx, pack)
        _products(ctx, pack, count)


def _brands(ctx: MasterContext, pack: IndustryPack) -> None:
    for brand in pack.brands:
        brand_id = ctx.ids.next("product_brands")
        ctx.ref("brand")[brand.code] = brand_id
        ctx.add("product_brands", {"product_brand_id": brand_id, "brand_code": brand.code, "brand_name": brand.name,
                                   "brand_tier": brand.tier, "is_private_label": brand.private_label, "is_active": True})


def _categories(ctx: MasterContext, pack: IndustryPack) -> None:
    bu_id = ctx.ref("business_unit")[pack.code]
    for category in pack.categories:                                  # parents first
        category_id = ctx.ids.next("product_categories")
        ctx.ref("category")[category.code] = category_id
        ctx.add("product_categories", {
            "product_category_id": category_id, "category_code": category.code, "category_name": category.name,
            "parent_category_id": ctx.ref("category")[category.parent_code] if category.parent_code else None,
            "business_unit_id": bu_id, "category_level": category.level, "is_leaf": category.is_leaf,
            "return_window_days": category.return_window_days, "is_active": True,
        })


def _launch_rate(ctx: MasterContext, leaf: Category, launched: date) -> Decimal:
    revision = ctx.seed.gst_rate_revision["effective_date"]
    if ctx.config.gst_rate_revision and launched >= revision:
        return leaf.gst_after
    return leaf.gst_before


@dataclass
class CatalogueCounters:
    """Per-pack numbering state for SKUs and styles, and product names in use (day 0 or loaded from the DB).

    Names are remembered as every word-prefix of every full product name, so the set can be rebuilt from
    the database exactly (D66): "Voltrix Pro Smartwatch Black" also marks "Voltrix Pro Smartwatch" as taken.
    """

    sku: int = 0
    style: int = 0
    names: set[str] = field(default_factory=set)

    def add_name(self, full_name: str) -> None:
        words = full_name.split(" ")
        for length in range(1, len(words) + 1):
            self.names.add(" ".join(words[:length]))

    def name_taken(self, name: str) -> bool:
        return name in self.names


def create_style(ctx: MasterContext, rng, pack: IndustryPack, leaf: Category, launched: date, max_skus: int,
                 counters: CatalogueCounters) -> list[dict[str, Any]]:
    """One product style (brand + series + noun) with up to ``max_skus`` variant × colour SKUs, their GST
    rate history and the ``products`` refs entries. Used on day 0 and for launches by the daily engine."""
    brand = rng.choice(pack.brands_for(leaf))
    series, noun = rng.choice(pack.name_series), rng.choice(leaf.nouns)
    name = f"{brand.name} {series} {noun}"
    model = 1
    while counters.name_taken(name):                                  # a second style of the same line
        model += 1
        name = f"{brand.name} {series} {noun} {model}"
    band = leaf.mrp[brand.tier]
    base_mrp = _uniform(rng, band.low, band.high, 0)
    variants = list(leaf.variants) or [None]
    colours = rng.sample(list(leaf.colours), k=min(len(leaf.colours), rng.randint(1, 2))) if leaf.colours else [None]
    skus = [(v, c) for v in variants for c in colours][:max_skus]
    style = None
    if len(skus) > 1:                      # only multi-SKU styles get a code, so the counter is derivable from the DB
        counters.style += 1
        style = codes.style_code(pack.business_unit_code, counters.style)
    ratio = _uniform(rng, pack.list_price_ratio.low, pack.list_price_ratio.high, 4)
    created = []
    for variant, colour in skus:
        counters.sku += 1
        mrp = charm_price(base_mrp * (variant.price_factor if variant else 1))
        launch_rate = _launch_rate(ctx, leaf, launched)
        ex_gst = sql_divide(mrp, 1 + launch_rate / 100, 4)
        list_price = (ex_gst * ratio).quantize(CENT, rounding=ROUND_DOWN)
        label = " ".join(part for part in (variant.label if variant else None, colour) if part)
        product_id = ctx.ids.next("products")
        base_units = max(2, min(200, int(Decimal(50_000) / mrp)))
        full_name = (f"{name} {label}" if label else name)[:200]
        counters.add_name(full_name)
        ctx.add("products", {
            "product_id": product_id, "sku": codes.sku(pack.business_unit_code, leaf.code, counters.sku),
            "product_name": full_name, "product_description": f"{leaf.name} by {brand.name}"[:500],
            "business_unit_id": ctx.ref("business_unit")[pack.code], "product_category_id": ctx.ref("category")[leaf.code],
            "product_brand_id": ctx.ref("brand")[brand.code], "base_unit_of_measure_id": ctx.ref("uom")[leaf.uom],
            "hsn_code": leaf.hsn, "style_code": style, "variant_size": variant.label[:20] if variant else None,
            "variant_colour": colour, "list_price": list_price, "mrp": mrp, "pack_size": None,
            "weight_kg": quantity(_uniform(rng, leaf.weight_kg.low, leaf.weight_kg.high, 3)),
            "volume_litres": quantity(_uniform(rng, leaf.volume_litres.low, leaf.volume_litres.high, 3)),
            "is_batch_tracked": leaf.has_expiry, "has_expiry": leaf.has_expiry, "shelf_life_days": leaf.shelf_life_days,
            "warranty_months": leaf.warranty_months, "is_returnable": leaf.returnable,
            "reorder_level": quantity(base_units), "reorder_quantity": quantity(base_units * 2), "launch_date": launched,
            "discontinued_date": None, "product_status": "ACTIVE", "deleted_at": None,
        })
        product = {"product_id": product_id, "pack": pack, "leaf": leaf, "list_price": list_price, "mrp": mrp,
                   "launched": launched}
        created.append(product)
        _tax_rates(ctx, product_id, leaf, launched)
    return created


def _products(ctx: MasterContext, pack: IndustryPack, count: int) -> None:
    rng = ctx.rng(f"products.{pack.code}")
    first_launch = ctx.refs["company"]["incorporated"] + timedelta(days=60)
    latest_launch = ctx.setup_day - timedelta(days=30)
    counters = ctx.refs.setdefault("catalogue_counters", {}).setdefault(pack.code, CatalogueCounters())
    for leaf, quota in zip(pack.leaves, _quotas(count, [leaf.share for leaf in pack.leaves])):
        made = 0
        while made < quota:
            launched = first_launch + timedelta(days=rng.randint(0, max(0, (latest_launch - first_launch).days)))
            created = create_style(ctx, rng, pack, leaf, launched, quota - made, counters)
            ctx.refs.setdefault("products", []).extend(created)
            made += len(created)


def _tax_rates(ctx: MasterContext, product_id: int, leaf: Category, launched: date) -> None:
    """INITIAL rate from launch; if the GST revision (D17) happened before day 0, the closed INITIAL period
    plus the REVISION period. A revision inside the history is applied by the daily engine on its date."""
    revision = ctx.seed.gst_rate_revision
    change = revision["effective_date"]
    periods: list[dict[str, Any]] = []
    if (ctx.config.gst_rate_revision and launched < change <= ctx.setup_day and leaf.gst_before != leaf.gst_after):
        periods.append({"gst_rate": rate(leaf.gst_before), "effective_from": launched,
                        "effective_to": change - timedelta(days=1), "change_reason": "INITIAL",
                        "notification_reference": None})
        periods.append({"gst_rate": rate(leaf.gst_after), "effective_from": change, "effective_to": None,
                        "change_reason": "GST_RATE_REVISION",
                        "notification_reference": revision["notification_reference"]})
    else:
        periods.append({"gst_rate": rate(_launch_rate(ctx, leaf, launched)), "effective_from": launched,
                        "effective_to": None, "change_reason": "INITIAL", "notification_reference": None})
    for period in periods:
        ctx.add("product_tax_rates", {"product_tax_rate_id": ctx.ids.next("product_tax_rates"),
                                      "product_id": product_id, **period})
