"""Business partners (1B §4.4-§4.7; step 3.5b): suppliers and their addresses, carriers, supplier ↔
product approvals, marketing campaigns, customers and their addresses.

* Goods suppliers belong to one industry pack: based in its hub cities, named with its suffixes.
  Service suppliers follow the plan in ``config/seed/reference.yaml`` (a landlord per warehouse, power and
  water per city, ISP, SaaS …); each carrier gets its own supplier.
* Every product gets 2-5 approved suppliers from its pack, one preferred (V-MD-15).
* Campaigns: one per pack sale event per year, for the configured years before day 0 and those
  starting within 30 days after it; owners are Marketing staff (V-MD-22).
* Customers: B2B with segment, GST registration, credit terms and limit, account manager and 1-4
  addresses; B2C with one home address. Acquisition channel from the pack mix; some acquisitions credited
  to a campaign whose window contains the date (V-MD-12). Consumers only if some pack sells to them.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from scmgen.core.money import money, quantity, unit_price
from scmgen.domain.identifiers import email, gstin, mobile, pan, pincode
from scmgen.generation.master import codes
from scmgen.generation.master.context import MasterContext, domain_slug
from scmgen.generation.master.organisation import CITY_PIN_SPREAD

LANDMARKS = ("Bus Stand", "Railway Station", "City Hospital", "Main Market", "Metro Station", "Post Office",
             "Government School", "Clock Tower")
HOLDER_TYPES = {"RETAILER": "PPFC", "WHOLESALER": "FFC", "CORPORATE": "C", "INSTITUTION": "TTC", "WORKSHOP": "PPF"}


def _pick(rng: random.Random, mix: dict[str, Any]) -> str:
    keys = sorted(mix)
    return rng.choices(keys, weights=[float(mix[k]) for k in keys])[0]


def _between(rng: random.Random, earliest: date, latest: date) -> date:
    return earliest + timedelta(days=rng.randint(0, max(0, (latest - earliest).days)))


class _Identifiers:
    """Unique PANs (and so GSTINs) across all parties of the build."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def pan(self, holder_type: str, name: str) -> str:
        while True:
            value = pan(self.rng, holder_type, name)
            if value not in self.used:
                self.used.add(value)
                return value


def _address(ctx: MasterContext, rng: random.Random, fake, city: dict[str, Any]) -> dict[str, Any]:
    return {"address_line1": f"{fake.building_number()}, {fake.street_name()}"[:200], "address_line2": None,
            "city": city["city"], "state_id": ctx.ref("state")[city["state_code"]],
            "pincode": pincode(rng, city["pin_prefix"], CITY_PIN_SPREAD)}


# ------------------------------------------------------------------------------------------ suppliers


def build_suppliers(ctx: MasterContext) -> None:
    rng, fake = ctx.rng("suppliers"), ctx.faker("suppliers")
    ids = ctx.refs.setdefault("pan_registry", _Identifiers(ctx.rng("pans")))
    settings = ctx.config.master_data
    company = ctx.refs["company"]
    onboard_from = company["incorporated"] + timedelta(days=30)
    onboard_to = ctx.setup_day - timedelta(days=60)
    terms = ctx.ref("payment_term")
    number = 0

    def add_supplier(name: str, supplier_type: str, registration: str, holder: str, term: str, city: dict[str, Any],
                     category: str | None, *, msme: str | None = None, dispatch_city: dict[str, Any] | None = None,
                     pack_code: str | None = None) -> int:
        nonlocal number
        number += 1
        supplier_id = ctx.ids.next("suppliers")
        holder_pan = ids.pan(holder, name)
        state = ctx.refs["state_row"][city["state_code"]]
        registered = registration in ("REGISTERED", "COMPOSITION")
        rating = Decimal(rng.randint(250, 500)) / 100 if supplier_type != "SERVICES" else Decimal(rng.randint(320, 480)) / 100
        onboarded = _between(rng, onboard_from, onboard_to)
        ctx.add("suppliers", {
            "supplier_id": supplier_id, "supplier_code": codes.supplier_code(number), "supplier_name": name[:200],
            "supplier_type": supplier_type, "gst_registration_type": registration,
            "gstin": gstin(state["gst_state_code"], holder_pan) if registered else None, "pan": holder_pan,
            "msme_category": msme, "payment_term_id": terms[term],
            "default_expense_category_id": ctx.ref("expense_category")[category] if category else None,
            "contact_person_name": fake.name()[:120], "email": email("sales", domain_slug(name)), "phone": mobile(rng),
            "supplier_rating": rating, "onboarded_date": onboarded, "supplier_status": "ACTIVE", "deleted_at": None,
        })
        for address_type, address_city in (("REGISTERED", city), ("DISPATCH", dispatch_city)):
            if address_city is None:
                continue
            ctx.add("supplier_addresses", {
                "supplier_address_id": ctx.ids.next("supplier_addresses"), "supplier_id": supplier_id,
                "address_type": address_type, "is_default": True, **_address(ctx, rng, fake, address_city),
                "contact_phone": mobile(rng), "previous_supplier_address_id": None, "valid_from": onboarded,
                "deleted_at": None,
            })
        ctx.ref("supplier")[supplier_id] = {"rating": rating, "onboarded": onboarded, "pack": pack_code,
                                            "code": codes.supplier_code(number)}
        return supplier_id

    # Service suppliers (structural) and carriers.
    warehouses = list(ctx.refs["warehouses"].values())
    cities = sorted({w.city["city"] for w in warehouses})
    for plan in ctx.seed.service_suppliers:
        if "count" in plan:
            locations = [ctx.city(rng.choice(cities)) for _ in range(plan["count"])]
        elif plan["per"] == "warehouse":
            locations = [w.city for w in warehouses]
        else:
            locations = [ctx.city(c) for c in cities]
        category = plan["category"]
        for city in locations:
            suffix = rng.choice(plan["suffixes"])
            if plan.get("utility"):
                add_supplier(f"{city['city']} {suffix}", "SERVICES", "UNREGISTERED", "G", "IMMEDIATE", city, category)
            elif rng.random() < plan.get("individual_share", 0):
                add_supplier(fake.name(), "SERVICES", "UNREGISTERED", "P", "IMMEDIATE", city, category)
            else:
                term = "ADVANCE" if category in ("SAAS", "AI_TOOLS", "CLOUD", "INSURANCE") else \
                    "IMMEDIATE" if category == "RENT" else rng.choice(("NET15", "NET30"))
                add_supplier(f"{fake.last_name()} {suffix}", "SERVICES", "REGISTERED", rng.choice("CF"), term, city,
                             category)
    for carrier in ctx.seed.shipping_carriers:
        category = "COURIER" if carrier["carrier_type"] in ("EXPRESS_COURIER", "HYPERLOCAL") else "FREIGHT_OUTWARD"
        supplier_id = add_supplier(f"{carrier['carrier_name']} Pvt. Ltd.", "SERVICES", "REGISTERED", "C", "NET30",
                                   ctx.city(rng.choice(cities)), category)
        carrier_id = ctx.ids.next("shipping_carriers")
        ctx.ref("carrier")[carrier["carrier_code"]] = carrier_id
        ctx.add("shipping_carriers", {"shipping_carrier_id": carrier_id, **carrier, "supplier_id": supplier_id})

    # Goods suppliers per pack, in proportion to the pack's products (at least enough for 2-5 per product).
    services = number
    goods_total = max(ctx.config.volumes.suppliers - services, settings.suppliers_per_product[1] * len(ctx.packs))
    products = ctx.refs.get("products", [])
    by_pack = {pack.code: sum(1 for p in products if p["pack"].code == pack.code) for pack in ctx.packs}
    weights = [max(by_pack[pack.code], 1) for pack in ctx.packs]
    for pack, weight in zip(ctx.packs, weights):
        count = max(settings.suppliers_per_product[1], round(goods_total * weight / sum(weights)))
        hubs = [ctx.city(c) for c in pack.supplier_hubs]
        for _ in range(count):
            registration = _pick(rng, settings.supplier_registration)
            msme = rng.choice(("MICRO", "SMALL", "MEDIUM")) if rng.random() < settings.msme_share else None
            term = rng.choice(("NET15", "NET30", "NET45") if msme else ("NET30", "NET45", "NET60"))
            city = rng.choice(hubs)
            dispatch = rng.choice(hubs) if rng.random() < settings.dispatch_address_share else None
            holder = rng.choice("CCF") if registration == "REGISTERED" else "P"
            name = f"{fake.last_name()} {rng.choice(pack.supplier_name_suffixes)}"
            if holder == "C":
                name += " Pvt. Ltd."
            supplier_id = add_supplier(name, "GOODS", registration, holder, term, city, None, msme=msme,
                                       dispatch_city=dispatch, pack_code=pack.code)
            ctx.ref("goods_suppliers").setdefault(pack.code, []).append(supplier_id)


def build_supplier_products(ctx: MasterContext) -> None:
    """2-5 approved suppliers per product from its pack, one preferred; cost from the pack margin (1B §4.5)."""
    approve_suppliers(ctx, ctx.rng("supplier_products"), ctx.refs.get("products", []))


def approve_suppliers(ctx: MasterContext, rng: random.Random, products: list[dict[str, Any]]) -> None:
    """Approve 2-5 of the pack's goods suppliers for each product (also used for launches by the engine)."""
    low, high = ctx.config.master_data.suppliers_per_product
    for product in products:
        pack = product["pack"]
        candidates = ctx.ref("goods_suppliers")[pack.code]
        chosen = rng.sample(candidates, k=min(len(candidates), rng.randint(low, high)))
        suppliers = ctx.ref("supplier")
        preferred = max(chosen, key=lambda s: (suppliers[s]["rating"] + Decimal(rng.random()) / 2, s))
        margin = Decimal(rng.randint(int(pack.gross_margin.low * 1000), int(pack.gross_margin.high * 1000))) / 1000
        lead_low, lead_high = pack.supplier_lead_time_days
        for supplier_id in chosen:
            info = suppliers[supplier_id]
            quality = info["rating"] - Decimal("3.75")
            cost = product["list_price"] * (1 - margin) * (1 + quality * Decimal("0.02")) \
                * Decimal(rng.randint(980, 1030)) / 1000
            mrp = product["mrp"]
            moq = rng.choice((24, 48, 100, 200) if mrp < 500 else (6, 10, 12, 20) if mrp < 5000 else (1, 2, 5))
            lead = max(1, min(120, round(rng.randint(lead_low, lead_high) * (1 - float(quality) * 0.1))))
            approved = _between(rng, max(info["onboarded"], product["launched"]), ctx.setup_day - timedelta(days=1))
            ctx.add("supplier_products", {
                "supplier_product_id": ctx.ids.next("supplier_products"), "supplier_id": supplier_id,
                "product_id": product["product_id"], "supplier_sku": f"{info['code'][4:]}-{product['product_id']:06d}",
                "reference_unit_cost": unit_price(money(cost)), "minimum_order_quantity": quantity(moq),
                "lead_time_days": lead, "is_preferred": supplier_id == preferred, "approved_date": approved,
                "supplier_product_status": "ACTIVE", "deleted_at": None,
            })


# ------------------------------------------------------------------------------------------ campaigns


def build_campaigns(ctx: MasterContext) -> None:
    """One campaign per pack sale event per year: the past ``history_years`` (COMPLETED, a few CANCELLED)
    and those starting within 30 days after day 0 (PLANNED); later ones come from the daily engine."""
    rng = ctx.rng("campaigns")
    settings = ctx.config.master_data
    owners = ctx.ref("employees_by_department").get("MKT", [])
    earliest = ctx.setup_day - timedelta(days=365 * settings.campaign_history_years)
    horizon = ctx.setup_day + timedelta(days=30)
    planned = []
    for pack in ctx.packs:
        for year in range(earliest.year, horizon.year + 1):
            for event in pack.events:
                start = date(year, event.month, event.day)
                end = start + timedelta(days=event.days - 1)
                if start > horizon or start < earliest:
                    continue
                planned.append((start, end, pack, event))
    per_year: dict[int, int] = {}
    for start, end, pack, event in sorted(planned, key=lambda p: (p[0], p[2].code, p[3].name)):
        per_year[start.year] = per_year.get(start.year, 0) + 1
        if start > ctx.setup_day:
            status = "PLANNED"
        elif end >= ctx.setup_day:
            status = "ACTIVE"
        else:
            status = "CANCELLED" if rng.random() < settings.campaign_cancelled_share else "COMPLETED"
        create_campaign(ctx, rng, pack, event, start, status, per_year[start.year], owners)


def create_campaign(ctx: MasterContext, rng: random.Random, pack, event, start: date, status: str, number: int,
                    owners: list[int]) -> int:
    """One campaign for a pack sale event (also used by the daily engine, 30 days ahead of the start)."""
    settings = ctx.config.master_data
    end = start + timedelta(days=event.days - 1)
    channel = rng.choice(event.channels)
    target = "B2B" if channel == "TRADE_SHOW" or pack.b2b_sales_share == 1 else "B2C"
    campaign_id = ctx.ids.next("marketing_campaigns")
    tag = "".join(c if c.isalpha() else "_" for c in event.name.upper()).strip("_")[:30]
    budget = Decimal(rng.randint(*settings.campaign_budget)) * event.uplift
    ctx.add("marketing_campaigns", {
        "marketing_campaign_id": campaign_id, "campaign_code": codes.campaign_code(start.year, number),
        "campaign_name": f"{event.name} {start.year}"[:150], "marketing_channel_id": ctx.ref("marketing_channel")[channel],
        "business_unit_id": ctx.ref("business_unit")[pack.code], "target_customer_type": target,
        "season_tag": tag if len(tag) >= 3 else None, "start_date": start, "end_date": end,
        "budget_amount": money(Decimal(round(budget / 1000) * 1000)), "owner_employee_id": rng.choice(owners),
        "campaign_status": status,
    })
    ctx.refs.setdefault("campaigns", []).append({"id": campaign_id, "channel": channel, "target": target,
                                                 "start": start, "end": end, "status": status, "pack": pack.code})
    return campaign_id


# ------------------------------------------------------------------------------------------ customers


def customer_cities(ctx: MasterContext, warehouse_states: set[str]) -> tuple[list[dict[str, Any]], list[float]]:
    """Cities customers live in, weighted by market size and more in regions with a warehouse (1B §4.4)."""
    settings = ctx.config.master_data
    regions = {ctx.refs["state_row"][state]["region"] for state in warehouse_states}
    cities = [c for c in ctx.seed.cities if c["state_code"] != "OT"]
    weights = [c["weight"] * (settings.warehouse_region_weight
                              if ctx.refs["state_row"][c["state_code"]]["region"] in regions else 1) for c in cities]
    return cities, weights


def build_customers(ctx: MasterContext) -> None:
    rng, fake = ctx.rng("customers"), ctx.faker("customers")
    ids = ctx.refs.setdefault("pan_registry", _Identifiers(ctx.rng("pans")))
    settings = ctx.config.master_data
    volumes = ctx.config.volumes
    company = ctx.refs["company"]
    b2b_packs = {p.code: p.b2b_sales_share for p in ctx.packs if p.b2b_sales_share > 0}
    b2c_packs = {p.code: 1 - p.b2b_sales_share for p in ctx.packs if p.b2b_sales_share < 1}
    packs = {p.code: p for p in ctx.packs}
    counts = {"B2B": volumes.customers_b2b if b2b_packs else 0, "B2C": volumes.customers_b2c if b2c_packs else 0}
    cities, weights = customer_cities(ctx, {w.city["state_code"] for w in ctx.refs["warehouses"].values()})
    sales_reps = ctx.refs.get("sales_executives") or ctx.ref("employees_by_department")["SAL"]
    campaigns = [c for c in ctx.refs.get("campaigns", []) if c["status"] != "CANCELLED" and c["start"] < ctx.setup_day]
    first_day = company["incorporated"] + timedelta(days=60)
    age_days = max(1, (ctx.setup_day - first_day).days)
    number = 0
    for kind in ("B2B", "B2C"):
        for _ in range(counts[kind]):
            number += 1
            pack = packs[_pick(rng, b2b_packs if kind == "B2B" else b2c_packs)]
            # Acquisition: exponential age (most customers are recent), bounded by the company's life.
            days_ago = min(age_days, 1 + int(rng.expovariate(1 / (settings.acquisition_mean_years * 365))))
            acquired = ctx.setup_day - timedelta(days=days_ago)
            channel = _pick(rng, pack.marketing_channels[kind])
            campaign = None
            if rng.random() < settings.campaign_attribution[kind]:
                fitting = [c for c in campaigns if c["target"] in (kind, "ALL") and c["pack"] == pack.code
                           and c["channel"] in pack.marketing_channels[kind]]
                if fitting:
                    chosen = rng.choice(fitting)
                    campaign, channel = chosen["id"], chosen["channel"]
                    acquired = _between(rng, chosen["start"], min(chosen["end"], ctx.setup_day - timedelta(days=1)))
            city = rng.choices(cities, weights=weights)[0]
            create_customer(ctx, rng, fake, ids, kind, pack, number, acquired, channel, campaign, city, cities,
                            weights, sales_reps)


def create_customer(ctx: MasterContext, rng: random.Random, fake, ids: "_Identifiers", kind: str, pack, number: int,
                    acquired: date, channel: str, campaign: int | None, city: dict[str, Any],
                    cities: list[dict[str, Any]], weights: list[float], sales_reps: list[int]) -> int:
    """One customer with its addresses (day 0, and new customers acquired during the history)."""
    settings = ctx.config.master_data
    customer_id = ctx.ids.next("customers")
    term = _pick(rng, pack.credit_terms[kind])
    due_days = next(t["due_days"] for t in ctx.seed.payment_terms if t["term_code"] == term)
    if kind == "B2B":
        segment = _pick(rng, settings.b2b_segments)
        registration = _pick(rng, settings.b2b_registration)
        surname = fake.last_name()
        suffix = rng.choice(settings.customer_name_suffixes[segment])
        name = f"{city['city']} {suffix}" if segment == "INSTITUTION" and rng.random() < 0.5 else f"{surname} {suffix}"
        holder = rng.choice(HOLDER_TYPES[segment])
        holder_pan = ids.pan(holder, name) if registration != "UNREGISTERED" or rng.random() < 0.6 else None
        customer_gstin = (gstin(ctx.refs["state_row"][city["state_code"]]["gst_state_code"], holder_pan)
                          if registration in ("REGISTERED", "COMPOSITION") else None)
        low, high = settings.credit_limits[segment]
        limit = money(Decimal(rng.randint(low // 10_000, high // 10_000) * 10_000)) if due_days > 0 else None
        row = {"customer_type": "B2B", "customer_segment": segment, "customer_name": name[:200],
               "contact_person_name": fake.name()[:120], "email": email("purchase", domain_slug(name)),
               "gst_registration_type": registration, "gstin": customer_gstin, "pan": holder_pan,
               "credit_limit": limit, "sales_employee_id": rng.choice(sales_reps)}
        contact = row["contact_person_name"]
    else:
        first, last = fake.first_name(), fake.last_name()
        row = {"customer_type": "B2C", "customer_segment": "CONSUMER", "customer_name": f"{first} {last}"[:200],
               "contact_person_name": None,
               "email": email(f"{first}.{last}{rng.randint(1, 99)}", "mailbox"),
               "gst_registration_type": "CONSUMER", "gstin": None, "pan": None, "credit_limit": None,
               "sales_employee_id": None}
        contact = row["customer_name"]
    phone = mobile(rng)
    ctx.add("customers", {
        "customer_id": customer_id, "customer_code": codes.customer_code(number), **row, "phone": phone,
        "payment_term_id": ctx.ref("payment_term")[term],
        "acquisition_marketing_channel_id": ctx.ref("marketing_channel")[channel],
        "acquisition_marketing_campaign_id": campaign, "acquisition_date": acquired,
        "customer_status": "ACTIVE", "deleted_at": None,
    })
    label = "Home" if kind == "B2C" else rng.choice(("Registered Office", "Shop", "Head Office"))
    addresses = [(label, True, city)]
    if kind == "B2B" and rng.random() < settings.extra_shipping_address_share:
        same_state = [c for c in cities if c["state_code"] == city["state_code"]]
        for n in range(rng.randint(1, 3)):
            other = rng.choice(same_state) if rng.random() < 0.7 else rng.choices(cities, weights=weights)[0]
            addresses.append((f"Delivery Point {n + 2} - {other['city']}"[:60], False, other))
    for address_label, is_main, address_city in addresses:
        ctx.add("customer_addresses", {
            "customer_address_id": ctx.ids.next("customer_addresses"), "customer_id": customer_id,
            "address_label": address_label, "is_billing_address": is_main, "is_shipping_address": True,
            "is_default_billing": is_main, "is_default_shipping": is_main, "contact_name": contact[:120],
            "contact_phone": phone if is_main else mobile(rng), **_address(ctx, rng, fake, address_city),
            "landmark": f"Near {rng.choice(LANDMARKS)}" if rng.random() < 0.3 else None,
            "previous_customer_address_id": None, "valid_from": acquired, "deleted_at": None,
        })
    return customer_id


def build_parties(ctx: MasterContext) -> None:
    """Step 3.5b parties, after the catalogue (suppliers need the products for their approvals)."""
    build_suppliers(ctx)
    build_supplier_products(ctx)
    build_campaigns(ctx)
    build_customers(ctx)
