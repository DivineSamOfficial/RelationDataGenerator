"""New customers over time (1B §4.4 "Over time"; 1G §2.2 step 3; step 3.8b).

Every day some B2B accounts and consumers are acquired: ``daily.new_customers_per_year`` × the preset's customer
count / 365, following the industry packs' seasonality. They are built by the same code as the day-0 customers
(``create_customer``): pack, segment, GST registration, credit terms and limit, addresses in cities weighted
towards the warehouse regions. Their acquisition channel comes from the pack's mix; some acquisitions are
credited to a campaign running on that day (V-MD-12). They join the demand model at once and can order the
same day.
"""

from __future__ import annotations

import re

from scmgen.generation.engine.day import DayContext, poisson
from scmgen.generation.engine.sales.model import add_customers
from scmgen.generation.master.parties import _Identifiers, _pick, create_customer, customer_cities


def _prepare(day: DayContext) -> None:
    """Master-data lookups the customer builder needs (derived from committed rows, D66)."""
    refs = day.world.refs
    if "customer_number" in refs:
        return
    refs["state"] = {r["state_code"]: r["state_id"] for r in day.rows("SELECT state_code, state_id FROM states")}
    refs["state_row"] = {row["state_code"]: row for row in day.world.seed.states}
    refs["payment_term"] = {r["term_code"]: r["payment_term_id"] for r in day.rows(
        "SELECT term_code, payment_term_id FROM payment_terms")}
    refs["used_pans"] = {r["pan"] for r in day.rows(
        "SELECT pan FROM customers WHERE pan IS NOT NULL UNION SELECT pan FROM suppliers WHERE pan IS NOT NULL "
        "UNION SELECT pan FROM companies WHERE pan IS NOT NULL")}
    refs["customer_warehouse_states"] = {r["state_code"] for r in day.rows(
        "SELECT DISTINCT s.state_code FROM warehouses w JOIN states s ON s.state_id = w.state_id")}
    refs["customer_number"] = max((int(re.search(r"([0-9]+)$", r["customer_code"]).group(1)) for r in day.rows(
        "SELECT customer_code FROM customers")), default=0)


def acquisition_step(day: DayContext) -> None:
    world = day.world
    settings = world.config.master_data
    rates = world.config.daily.new_customers_per_year
    volumes = world.config.volumes
    month = day.day.month - 1
    rng = day.rng("customers.acquisition")
    plans = []
    for kind in ("B2B", "B2C"):
        shares = {p.code: (p.b2b_sales_share if kind == "B2B" else 1 - p.b2b_sales_share) for p in world.packs}
        shares = {code: share for code, share in shares.items() if share > 0}
        if not shares:
            continue
        season = sum(float(world.packs_by_code[c].seasonality[month]) for c in shares) / len(shares)
        base = volumes.customers_b2b if kind == "B2B" else volumes.customers_b2c
        for _ in range(poisson(rng, base * rates[kind] / 365 * season)):
            plans.append((kind, world.packs_by_code[_pick(rng, shares)]))
    if not plans:
        return
    _prepare(day)
    refs = world.refs
    master = day.master()
    fake = master.faker("customers.acquired")
    ids = _Identifiers(rng)
    ids.used = refs["used_pans"]
    cities, weights = customer_cities(master, refs["customer_warehouse_states"])
    reps = refs["employees_by_department"]["SAL"]
    campaigns = day.rows("""
        SELECT c.marketing_campaign_id, c.business_unit_id, c.target_customer_type, m.channel_code
        FROM marketing_campaigns c JOIN marketing_channels m ON m.marketing_channel_id = c.marketing_channel_id
        WHERE c.campaign_status = 'ACTIVE' AND :d BETWEEN c.start_date AND c.end_date
        ORDER BY c.marketing_campaign_id""", d=day.day)
    created = []
    for kind, pack in plans:
        channel = _pick(rng, pack.marketing_channels[kind])
        campaign = None
        if rng.random() < settings.campaign_attribution[kind]:
            fitting = [c for c in campaigns if c["target_customer_type"] in (kind, "ALL")
                       and c["business_unit_id"] == refs["business_unit"][pack.code]
                       and c["channel_code"] in pack.marketing_channels[kind]]
            if fitting:
                chosen = rng.choice(fitting)
                campaign, channel = chosen["marketing_campaign_id"], chosen["channel_code"]
        refs["customer_number"] += 1
        city = rng.choices(cities, weights=weights)[0]
        created.append(create_customer(master, rng, fake, ids, kind, pack, refs["customer_number"], day.day, channel,
                                       campaign, city, cities, weights, reps))
    add_customers(day, created)
