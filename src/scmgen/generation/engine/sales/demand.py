"""New customer orders (1D §3 ``orders``; 1G §2.3 demand model; D10, D23, D37, D39, D42, D79; step 3.8a).

**How many.** Per industry pack and customer type, the expected orders of the day are

    orders_per_day × pack share × monthly seasonality × trend × weekday (holiday) factor × campaign uplift × type share

* *pack share*: the pack's share of the active catalogue;
* *type share*: the B2B / B2C split of the order **count** that gives B2B the pack's share of sales **value**
  (D23). It follows from the expected order values: B2B baskets are many times larger than B2C ones;
* *campaign uplift*: the pack event's uplift while a (non-cancelled) campaign for it runs, for the campaign's
  target customer type. The extra orders are attributed to the campaign.

**What.** Each order: a sales channel from the pack's mix (store counters only in store cities and on working
days), a customer (pool weights, see ``model``), a fulfilment type, an hour from the type's profile, a last-touch
marketing channel (and campaign when one runs on it), products by popularity (counter and pickup orders only
from the store's available stock), quantities by segment, prices:

* B2C lines are priced **including GST** (D79): the shelf price is the list price plus GST on the order date,
  rounded down to the rupee and never above MRP; campaign orders carry a promo discount;
* B2B lines are priced at list price excluding GST, with segment (wholesaler) and volume discounts.

Orders carry the total GST only (D39). Shipped B2C orders below the free-shipping threshold pay a shipping
charge (D42); the estimate includes its GST at the highest goods rate and is rounded to the nearest rupee (what a prepaid
customer pays; the invoice round-off then makes the invoice total equal to it, step 3.9).
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from scmgen.core.money import money, whole_rupees
from scmgen.domain.gst import price_exclusive, price_inclusive, shipping_charge_rate, total_gst
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import preload_rates, rate_on
from scmgen.generation.engine.sales.allocation import expired_stock
from scmgen.generation.engine.sales.model import Customer, SalesModel, lognormal_mean, popularity
from scmgen.generation.packs import IndustryPack

B2B_LINE_SIGMA = 0.6                       # spread of a B2B line's value around the segment median
ASSISTED_CHANNELS = ("FIELD_SALES", "PHONE", "WHATSAPP")   # B2B orders taken by the account manager
PICKUP_CHANNELS = ("WEBSITE", "MOBILE_APP")


@dataclass
class Draft:
    """An order before it gets its id: everything drawn, priced later in one pass."""
    pack: IndustryPack
    customer: Customer
    placed_at: datetime
    sales_channel: str
    fulfilment: str                         # SHIP / STORE_PICKUP / COUNTER_SALE
    store_id: int | None
    marketing_channel: str
    campaign_id: int | None
    items: list[dict[str, Any]]             # product_id, quantity, discount_percent, discount_type
    payment_term: str = ""
    sales_employee_id: int | None = None
    shipping_address_id: int | None = None
    customer_po_reference: str | None = None
    requested_delivery_date: date | None = None
    cod: bool = False                       # the customer would pay cash on delivery (if allowed)
    lines: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, Decimal] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------ per-day inputs


def _active_campaigns(day: DayContext) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Running campaigns per (pack, customer type), each with its event's uplift."""
    world = day.world
    pack_of_bu = {bu: code for code, bu in world.refs["business_unit"].items()}
    events = {(pack.code, event.name): event for pack in world.packs for event in pack.events}
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in day.rows("""
            SELECT c.marketing_campaign_id, c.campaign_name, c.business_unit_id, c.target_customer_type,
                   m.channel_code FROM marketing_campaigns c
            JOIN marketing_channels m ON m.marketing_channel_id = c.marketing_channel_id
            WHERE c.campaign_status = 'ACTIVE' AND :d BETWEEN c.start_date AND c.end_date
            ORDER BY c.marketing_campaign_id""", d=day.day):
        pack = pack_of_bu.get(row["business_unit_id"])
        event = events.get((pack, row["campaign_name"].rsplit(" ", 1)[0]))
        uplift = float(event.uplift) if event else 1.0
        for kind in (("B2B", "B2C") if row["target_customer_type"] == "ALL" else (row["target_customer_type"],)):
            found.setdefault((pack, kind), []).append({"id": row["marketing_campaign_id"],
                                                       "channel": row["channel_code"], "uplift": uplift})
    return found


@dataclass
class Assortment:
    """The products a pack sells today, with cumulative popularity weights."""
    products: list[int]
    cumulative: list[float]
    mean_list_price: float

    def pick(self, rng: random.Random) -> int:
        return self.products[bisect_right(self.cumulative, rng.random() * self.cumulative[-1])]


def _product_rows(day: DayContext, model: SalesModel, product_ids: list[int]) -> dict[int, dict[str, Any]]:
    """List price, MRP and category of products (immutable attributes, cached for the run)."""
    cache = model.product_rows
    missing = [p for p in product_ids if p not in cache]
    for start in range(0, len(missing), 2000):
        chunk = missing[start:start + 2000]
        for row in day.rows(f"""
                SELECT p.product_id, p.list_price, p.mrp, c.category_code FROM products p
                JOIN product_categories c ON c.product_category_id = p.product_category_id
                WHERE p.product_id IN ({", ".join(map(str, chunk))})"""):
            cache[row["product_id"]] = row
    return cache


def _assortments(day: DayContext, model: SalesModel) -> dict[str, Assortment]:
    world = day.world
    month = day.day.month - 1
    result = {}
    for pack in world.packs:
        active = [(p, launched) for p, launched in world.active_products.get(pack.code, []) if launched <= day.day]
        rows = _product_rows(day, model, [p for p, _ in active])
        products, weights, priced = [], [], 0.0
        pack_factor = float(pack.seasonality[month])
        for product_id, launched in active:
            _, leaf = world.leaf_of_category[rows[product_id]["category_code"]]
            season = float(leaf.seasonality[month]) / pack_factor if leaf.seasonality and pack_factor else 1.0
            weight = popularity(day, model, product_id, launched) * season
            products.append(product_id)
            weights.append(weight)
            priced += weight * float(rows[product_id]["list_price"])
        if products:
            cumulative = []
            running = 0.0
            for weight in weights:
                running += weight
                cumulative.append(running)
            result[pack.code] = Assortment(products, cumulative, priced / running)
    return result


def _store_stock(day: DayContext, model: SalesModel) -> dict[int, dict[int, Decimal]]:
    """Whole units on the shelf today at each store (sellable − reserved − expired batches)."""
    if not model.stores:
        return {}
    expired = expired_stock(day)
    stock: dict[int, dict[int, Decimal]] = {store: {} for store in model.stores.values()}
    for row in day.rows(f"""
            SELECT warehouse_id, product_id, qty_sellable - qty_reserved AS available FROM inventory
            WHERE warehouse_id IN ({", ".join(map(str, model.stores.values()))}) AND qty_sellable - qty_reserved >= 1
            ORDER BY warehouse_id, product_id"""):
        units = (row["available"] - expired.get((row["warehouse_id"], row["product_id"]), Decimal(0))).to_integral_value(
            rounding=ROUND_FLOOR)
        if units >= 1:
            stock[row["warehouse_id"]][row["product_id"]] = units
    return stock


def _store_staff(day: DayContext) -> dict[int, list[int]]:
    staff: dict[int, list[int]] = {}
    for row in day.rows("""
            SELECT e.warehouse_id, e.employee_id FROM employees e JOIN warehouses w ON w.warehouse_id = e.warehouse_id
            WHERE w.warehouse_type = 'RETAIL_STORE' AND e.employment_status = 'ACTIVE' AND e.hire_date <= :d
            ORDER BY e.warehouse_id, e.employee_id""", d=day.day):
        staff.setdefault(row["warehouse_id"], []).append(row["employee_id"])
    return staff


# ------------------------------------------------------------------------------------------ how many


def _b2b_order_value(day: DayContext, model: SalesModel, pack: str) -> float:
    """Expected ex-GST value of a B2B order of the pack, over its customers weighted by how often they order
    (segment basket, capped at the account's share of its credit limit)."""
    if pack not in model.b2b_order_value:
        pool = model.pools.get((pack, "B2B"))
        settings = day.world.config.sales
        lines = sum(settings.b2b_lines) / 2
        if not pool or not pool.customers:
            model.b2b_order_value[pack] = 0.0
        else:
            weights = [b - a for a, b in zip([0.0] + pool.cumulative[:-1], pool.cumulative)]
            values = []
            for customer in pool.customers:
                basket = lines * settings.b2b_line_value[customer.segment] * lognormal_mean(B2B_LINE_SIGMA)
                if customer.credit_limit:
                    basket = min(basket, float(customer.credit_limit) * settings.b2b_max_order_share)
                values.append(basket)
            model.b2b_order_value[pack] = sum(w * v for w, v in zip(weights, values)) / sum(weights)
    return model.b2b_order_value[pack]


def expected_orders(day: DayContext, model: SalesModel, assortments: dict[str, Assortment],
                    campaigns: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[tuple[str, str], float]:
    """Expected orders today per (pack, customer type)."""
    world = day.world
    settings = world.config.sales
    total_products = sum(len(a.products) for a in assortments.values())
    years = ((day.day - world.first_day).days if world.first_day else 0) / 365.25
    trend = (1 + settings.trend_per_year) ** years
    holiday = (day.day.month, day.day.day) in world.config.daily.national_holidays
    b2c_lines = sum(n * s for n, s in settings.b2c_lines.items())
    b2c_quantity = sum(n * s for n, s in settings.b2c_quantity.items())
    b2b_lines = sum(settings.b2b_lines) / 2
    result = {}
    for pack in world.packs:
        assortment = assortments.get(pack.code)
        if assortment is None:
            continue
        base = (world.config.volumes.orders_per_day * len(assortment.products) / total_products
                * float(pack.seasonality[day.day.month - 1]) * trend)
        share = float(pack.b2b_sales_share)
        b2c_value = b2c_lines * b2c_quantity * assortment.mean_list_price
        b2b_value = max(_b2b_order_value(day, model, pack.code), b2b_lines * assortment.mean_list_price)
        b2b_count = share * b2c_value / (share * b2c_value + (1 - share) * b2b_value) if b2b_value else 0.0
        for kind, count_share in (("B2B", b2b_count), ("B2C", 1 - b2b_count)):
            if count_share <= 0:
                continue
            factor = settings.weekday_factors[kind][day.day.weekday()]
            if holiday:
                factor *= settings.holiday_factor[kind]
            uplift = max((c["uplift"] for c in campaigns.get((pack.code, kind), [])), default=1.0)
            result[(pack.code, kind)] = base * count_share * factor * uplift
    return result


def _count(rng: random.Random, mean: float) -> int:
    """Poisson count (normal approximation for large means)."""
    if mean <= 0:
        return 0
    if mean > 60:
        return max(0, round(rng.gauss(mean, math.sqrt(mean))))
    limit, count, product = math.exp(-mean), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


# ------------------------------------------------------------------------------------------ what


def _pick(rng: random.Random, mix: dict[Any, Any]) -> Any:
    keys = list(mix)
    return rng.choices(keys, weights=[float(mix[k]) for k in keys])[0]


def _time(day: DayContext, rng: random.Random, hours: tuple[float, ...]) -> datetime:
    hour = rng.choices(range(24), weights=hours)[0]
    return day.at(hour, rng.randint(0, 59), rng.randint(0, 59))


def _draft(day: DayContext, rng: random.Random, model: SalesModel, pack: IndustryPack, kind: str,
           assortment: Assortment, campaigns: list[dict[str, Any]], store_stock: dict[int, dict[int, Decimal]],
           staff: dict[int, list[int]]) -> Draft | None:
    settings = day.world.config.sales
    channels = dict(pack.sales_channels[kind])
    store_open = day.is_working_day and any(model.store_pools.get((pack.code, s)) and
                                            model.store_pools[(pack.code, s)].customers for s in model.stores.values())
    if not store_open:
        channels.pop("RETAIL_STORE", None)
    channels = {code: share for code, share in channels.items() if code in model.sales_channels}
    if not channels:
        return None
    channel = _pick(rng, channels)

    store_id, fulfilment = None, "SHIP"
    if channel == "RETAIL_STORE":
        stores = [s for s in model.stores.values() if model.store_pools[(pack.code, s)].customers]
        store_id = rng.choices(stores, weights=[model.store_pools[(pack.code, s)].cumulative[-1] for s in stores])[0]
        customer = model.store_pools[(pack.code, store_id)].pick(rng, day.day)
        fulfilment = "COUNTER_SALE"
    else:
        pool = model.pools.get((pack.code, kind))
        customer = pool.pick(rng, day.day) if pool else None
        if (customer is not None and kind == "B2C" and channel in PICKUP_CHANNELS and customer.city in model.stores
                and rng.random() < settings.store_pickup_share):
            store_id, fulfilment = model.stores[customer.city], "STORE_PICKUP"
    if customer is None:
        return None
    placed_at = _time(day, rng, settings.hours["STORE" if fulfilment == "COUNTER_SALE" else kind])

    # Last-touch attribution: campaign-driven orders (the uplift) carry their campaign (A1).
    uplift = max((c["uplift"] for c in campaigns), default=1.0)
    campaign_id = None
    if campaigns and rng.random() < (uplift - 1) / uplift:
        chosen = rng.choice(campaigns)
        marketing, campaign_id = chosen["channel"], chosen["id"]
    else:
        marketing = _pick(rng, pack.marketing_channels[kind])
        running = [c for c in campaigns if c["channel"] == marketing]
        if running and rng.random() < settings.campaign_attribution_share:
            campaign_id = rng.choice(running)["id"]

    # Products: by popularity; store orders only what the store has on the shelf today.
    stock = store_stock.get(store_id, {}) if store_id else None
    shelf = None
    if stock is not None:
        shelf = [p for p in assortment.products if stock.get(p, 0) >= 1]
        if not shelf:
            if fulfilment == "COUNTER_SALE":
                return None
            store_id, fulfilment, stock, shelf = None, "SHIP", None, None
    lines = _pick(rng, settings.b2c_lines) if kind == "B2C" else rng.randint(*settings.b2b_lines)
    chosen_products: list[int] = []
    for _ in range(lines * 5):
        if len(chosen_products) == lines:
            break
        product_id = rng.choice(shelf) if shelf is not None else assortment.pick(rng)
        if product_id not in chosen_products:
            chosen_products.append(product_id)
    rows = model.product_rows
    items = []
    for product_id in chosen_products:
        if kind == "B2C":
            qty = Decimal(_pick(rng, settings.b2c_quantity))
        else:
            value = settings.b2b_line_value[customer.segment] * rng.lognormvariate(0, B2B_LINE_SIGMA)
            qty = Decimal(max(1, round(value / float(rows[product_id]["list_price"]))))
        if stock is not None:
            qty = min(qty, stock[product_id])
            stock[product_id] -= qty                 # the shelf empties as the day goes on
            if stock[product_id] < 1:
                del stock[product_id]
        items.append({"product_id": product_id, "quantity": qty})
    if not items:
        return None
    if kind == "B2B" and customer.credit_limit:
        # Orders are sized to the account (its credit limit follows its volume): scale big baskets down.
        ceiling = float(customer.credit_limit) * settings.b2b_max_order_share
        value = sum(float(i["quantity"] * rows[i["product_id"]]["list_price"]) for i in items)
        if value > ceiling:
            for item in items:
                item["quantity"] = Decimal(max(1, int(float(item["quantity"]) * ceiling / value)))

    draft = Draft(pack=pack, customer=customer, placed_at=placed_at, sales_channel=channel, fulfilment=fulfilment,
                  store_id=store_id, marketing_channel=marketing, campaign_id=campaign_id, items=items)
    if fulfilment == "SHIP":
        addresses = customer.shipping_address_ids
        draft.shipping_address_id = addresses[0] if len(addresses) == 1 or rng.random() < 0.7 else rng.choice(addresses)
    if kind == "B2B":
        if channel in ASSISTED_CHANNELS:
            draft.sales_employee_id = customer.sales_employee_id
        if rng.random() < 0.7:
            draft.customer_po_reference = f"PO/{day.day:%y}/{rng.randint(100, 99999):05d}"
        if rng.random() < settings.requested_delivery_share:
            draft.requested_delivery_date = day.day + timedelta(days=rng.randint(*settings.requested_delivery_days))
    elif fulfilment == "COUNTER_SALE" and staff.get(store_id):
        draft.sales_employee_id = rng.choice(staff[store_id])
    _discounts(rng, draft, settings, rows)
    return draft


def _discounts(rng: random.Random, draft: Draft, settings, rows: dict[int, dict[str, Any]]) -> None:
    """Campaign promo (B2C campaign orders), segment price (wholesalers), volume discount (large B2B lines)."""
    promo = rng.randint(*settings.campaign_discount) if draft.campaign_id else 0
    segment = rng.randint(*settings.wholesaler_discount) if draft.customer.segment == "WHOLESALER" else 0
    volume = rng.randint(*settings.volume_discount)
    for item in draft.items:
        options = [(0, None)]
        if draft.customer.kind == "B2C":
            options.append((promo, "CAMPAIGN_PROMO"))
        else:
            options.append((segment, "SEGMENT"))
            if item["quantity"] * rows[item["product_id"]]["list_price"] >= settings.volume_discount_min_value:
                options.append((volume, "VOLUME"))
        percent, kind = max(options, key=lambda o: o[0])
        item["discount_percent"], item["discount_type"] = Decimal(percent), (kind if percent > 0 else None)


# ------------------------------------------------------------------------------------------ prices


def shelf_price(list_price: Decimal, gst_rate: Decimal, mrp: Decimal) -> Decimal:
    """B2C shelf price incl. GST (D79): list price + GST, rounded down to the rupee, never above MRP."""
    price = (list_price * (1 + gst_rate / 100)).to_integral_value(rounding=ROUND_FLOOR)
    return max(Decimal(1), min(price, mrp.to_integral_value(rounding=ROUND_FLOOR)))


def price_draft(day: DayContext, model: SalesModel, draft: Draft) -> None:
    """Line amounts (D79 / D84 formulas through ``scmgen.domain.gst``), header totals and payment term."""
    settings = day.world.config.sales
    rows = model.product_rows
    inclusive = draft.customer.kind == "B2C"
    lines = []
    for number, item in enumerate(draft.items, start=1):
        row = rows[item["product_id"]]
        gst_rate = rate_on(day, item["product_id"], day.day)
        if inclusive:
            priced = price_inclusive(item["quantity"], shelf_price(row["list_price"], gst_rate, row["mrp"]),
                                     item["discount_percent"], gst_rate)
        else:
            priced = price_exclusive(item["quantity"], row["list_price"], item["discount_percent"], gst_rate, 90)
        tax = total_gst(priced.taxable_amount, gst_rate)
        lines.append({
            "line_number": number, "product_id": item["product_id"], "quantity_ordered": priced.quantity,
            "list_price": row["list_price"], "unit_price": priced.unit_price,
            "inclusive_unit_price": priced.inclusive_unit_price, "discount_percent": priced.discount_percent,
            "discount_type": item["discount_type"], "taxable_amount": priced.taxable_amount, "gst_rate": gst_rate,
            "tax_amount": tax, "line_total": priced.taxable_amount + tax,
        })
    taxable = sum((l["taxable_amount"] for l in lines), Decimal(0))
    tax = sum((l["tax_amount"] for l in lines), Decimal(0))
    shipping = Decimal(0)
    if inclusive and draft.fulfilment == "SHIP" and taxable + tax < settings.b2c_free_shipping_above:
        shipping = money(settings.b2c_shipping_charge)
    shipping_tax = total_gst(shipping, shipping_charge_rate(l["gst_rate"] for l in lines)) if shipping else Decimal(0)
    estimate = whole_rupees(taxable + tax + shipping + shipping_tax)
    draft.lines = lines
    draft.totals = {"items_taxable_amount": taxable, "items_tax_amount": tax, "shipping_charge_amount": shipping,
                    "order_total_estimate": estimate}
    if draft.customer.kind == "B2B":
        draft.payment_term = next(code for code, t in model.payment_terms.items()
                                  if t["payment_term_id"] == draft.customer.payment_term_id)
    elif draft.fulfilment == "COUNTER_SALE":
        draft.payment_term = "IMMEDIATE"
    elif draft.fulfilment == "SHIP" and estimate <= settings.cod_max_value and draft.cod:
        draft.payment_term = "COD"
    else:
        draft.payment_term = "ADVANCE"


def draft_orders(day: DayContext, model: SalesModel) -> list[Draft]:
    """Today's new orders, drawn per pack and customer type and priced, in the order they were placed."""
    assortments = _assortments(day, model)
    campaigns = _active_campaigns(day)
    expected = expected_orders(day, model, assortments, campaigns)
    store_stock = _store_stock(day, model)
    staff = _store_staff(day)
    settings = day.world.config.sales
    drafts: list[Draft] = []
    for (pack_code, kind), mean in expected.items():
        rng = day.rng(f"sales.orders.{pack_code}.{kind.lower()}")
        pack = day.world.packs_by_code[pack_code]
        for _ in range(_count(rng, mean)):
            draft = _draft(day, rng, model, pack, kind, assortments[pack_code], campaigns.get((pack_code, kind), []),
                           store_stock, staff)
            if draft is not None:
                draft.cod = rng.random() < settings.cod_share
                drafts.append(draft)
    preload_rates(day, {i["product_id"] for d in drafts for i in d.items}, day.day)
    for draft in drafts:
        price_draft(day, model, draft)
    drafts.sort(key=lambda d: (d.placed_at, d.customer.customer_id))
    return drafts
