"""The demand side's view of customers, products and stores (1G §2.3 "Demand", "Customer behaviour").

Built from committed rows on the first sales day of a run and kept in ``world.refs`` (D66: derivable, so a
split run rebuilds exactly the same model). Every per-customer and per-product trait comes from a stream
keyed by that customer or product (D65), never from the order in which rows were loaded:

* **Customer weight** (how often the customer orders): for B2B accounts proportional to the credit limit, with a
  log-normal spread. Credit limits are set by purchase volume in real companies, so a busy account doesn't sit
  on credit hold all the time; together with the bigger baskets of wholesalers, a minority of accounts drives
  most of the B2B value. Consumers have a log-normal buying propensity, which produces repeat buyers without
  a hidden counter.
* **Home pack**: the industry pack most of a customer's orders come from (``pack_affinity``).
* **Product popularity**: log-normal weight within its business unit, reshaped by the category's own
  seasonality when the pack defines one.
"""

from __future__ import annotations

import itertools
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from scmgen.generation.engine.day import DayContext
from scmgen.generation.packs import IndustryPack


@dataclass
class Customer:
    customer_id: int
    kind: str                         # B2B / B2C
    segment: str
    acquired: date
    payment_term_id: int
    credit_limit: Decimal | None      # None: no limit (immediate-payment accounts)
    sales_employee_id: int | None
    billing_address_id: int
    city: str
    shipping_address_ids: list[int]   # default first
    weight: float
    home_pack: str


@dataclass
class Pool:
    """Customers that can place a pack's orders of one type, with cumulative weights for ``choices``."""
    customers: list[Customer]
    cumulative: list[float]

    def pick(self, rng, day: date) -> Customer | None:
        """A customer acquired before ``day`` (customers acquired during the history order from the next day)."""
        if not self.customers:
            return None
        for _ in range(20):
            chosen = self.customers[bisect_right(self.cumulative, rng.random() * self.cumulative[-1])]
            if chosen.acquired < day:
                return chosen
        return None


@dataclass
class SalesModel:
    customers: dict[int, Customer]
    pools: dict[tuple[str, str], Pool]                       # (pack, type) → pool
    store_pools: dict[tuple[str, int], Pool]                 # (pack, store warehouse) → B2C customers of that city
    stores: dict[str, int]                                   # city → RETAIL_STORE warehouse id
    sales_channels: dict[str, dict[str, Any]]                # code → row
    address_city: dict[int, str]                             # customer address id → city
    payment_terms: dict[str, dict[str, Any]]                 # code → row
    popularity: dict[int, float] = field(default_factory=dict)   # product id → stable weight
    product_rows: dict[int, dict[str, Any]] = field(default_factory=dict)   # list price, MRP, category
    b2b_order_value: dict[str, float] = field(default_factory=dict)         # pack → expected B2B order value


def _home_pack(rng, packs: list[IndustryPack], kind: str) -> str | None:
    shares = [(p.code, float(p.b2b_sales_share if kind == "B2B" else 1 - p.b2b_sales_share)) for p in packs]
    shares = [(code, share) for code, share in shares if share > 0]
    if not shares:
        return None
    return rng.choices([c for c, _ in shares], weights=[s for _, s in shares])[0]


def _pool(customers: list[Customer], pack: str, others: int, affinity: float) -> Pool:
    away = (1 - affinity) / others if others else 0.0
    chosen, weights = [], []
    for customer in customers:
        factor = affinity if customer.home_pack == pack else away
        if factor > 0:
            chosen.append(customer)
            weights.append(customer.weight * factor)
    return Pool(chosen, list(itertools.accumulate(weights)))


def _load_customers(day: DayContext, only: list[int] | None = None) -> tuple[dict[int, Customer], dict[int, str]]:
    """Active customers (all, or the given ids) with their live addresses, as the demand model sees them."""
    world = day.world
    settings = world.config.sales
    filter_ = f"AND customer_id IN ({', '.join(map(str, only))})" if only else ""
    addresses: dict[int, dict[str, Any]] = {}
    address_city: dict[int, str] = {}
    for row in day.rows(f"""
            SELECT customer_address_id, customer_id, city, is_default_billing, is_default_shipping,
                   is_shipping_address FROM customer_addresses
            WHERE deleted_at IS NULL {filter_} ORDER BY customer_id, customer_address_id"""):
        address_city[row["customer_address_id"]] = row["city"]
        entry = addresses.setdefault(row["customer_id"], {"billing": None, "city": None, "shipping": []})
        if row["is_default_billing"]:
            entry["billing"], entry["city"] = row["customer_address_id"], row["city"]
        if row["is_default_shipping"]:
            entry["shipping"].insert(0, row["customer_address_id"])
        elif row["is_shipping_address"]:
            entry["shipping"].append(row["customer_address_id"])
    customers: dict[int, Customer] = {}
    for row in day.rows(f"""
            SELECT customer_id, customer_type, customer_segment, acquisition_date, payment_term_id, credit_limit,
                   sales_employee_id FROM customers
            WHERE customer_status = 'ACTIVE' AND deleted_at IS NULL {filter_} ORDER BY customer_id"""):
        rng = day.streams.stream(row["acquisition_date"], f"sales.customer.{row['customer_id']}")
        if row["customer_type"] == "B2B":
            low, high = world.config.master_data.credit_limits[row["customer_segment"]]
            limit = float(row["credit_limit"]) if row["credit_limit"] else (low + high) / 2
            weight = limit / 1e6 * rng.lognormvariate(0, settings.b2b_frequency_sigma)
        else:
            weight = rng.lognormvariate(0, 1)
        home = _home_pack(rng, world.packs, row["customer_type"])
        address = addresses.get(row["customer_id"])
        if home is None or address is None or address["billing"] is None:
            continue
        customers[row["customer_id"]] = Customer(
            customer_id=row["customer_id"], kind=row["customer_type"], segment=row["customer_segment"],
            acquired=row["acquisition_date"], payment_term_id=row["payment_term_id"],
            credit_limit=row["credit_limit"], sales_employee_id=row["sales_employee_id"],
            billing_address_id=address["billing"], city=address["city"],
            shipping_address_ids=address["shipping"] or [address["billing"]], weight=weight, home_pack=home)
    return customers, address_city


def _build_pools(day: DayContext, model: SalesModel) -> None:
    world = day.world
    settings = world.config.sales
    model.pools, model.store_pools = {}, {}
    model.b2b_order_value.clear()                 # depends on the pools
    for kind in ("B2B", "B2C"):
        of_kind = [c for c in model.customers.values() if c.kind == kind]
        serving = [p for p in world.packs if (p.b2b_sales_share if kind == "B2B" else 1 - p.b2b_sales_share) > 0]
        for pack in serving:
            model.pools[(pack.code, kind)] = _pool(of_kind, pack.code, len(serving) - 1, settings.pack_affinity)
            if kind == "B2C":
                for city, store in model.stores.items():
                    model.store_pools[(pack.code, store)] = _pool([c for c in of_kind if c.city == city], pack.code,
                                                                  len(serving) - 1, settings.pack_affinity)


def sales_model(day: DayContext) -> SalesModel:
    """The model of this run, built once from the database (D66)."""
    world = day.world
    if "sales_model" in world.refs:
        return world.refs["sales_model"]
    customers, address_city = _load_customers(day)
    stores = {r["city"]: r["warehouse_id"] for r in day.rows(
        "SELECT warehouse_id, city FROM warehouses WHERE warehouse_type = 'RETAIL_STORE' "
        "AND warehouse_status = 'ACTIVE' ORDER BY warehouse_id")}
    channels = {r["channel_code"]: r for r in day.rows(
        "SELECT sales_channel_id, channel_code, requires_shipment, allowed_customer_type FROM sales_channels "
        "WHERE is_active")}
    terms = {r["term_code"]: r for r in day.rows(
        "SELECT payment_term_id, term_code, due_days, is_prepaid FROM payment_terms")}
    model = SalesModel(customers=customers, pools={}, store_pools={}, stores=stores, sales_channels=channels,
                       payment_terms=terms, address_city=address_city)
    _build_pools(day, model)
    world.refs["sales_model"] = model
    return model


def add_customers(day: DayContext, customer_ids: list[int]) -> None:
    """New customers join a model that is already built (exactly as a rebuild from the database would see them)."""
    model = day.world.refs.get("sales_model")
    if model is None or not customer_ids:
        return
    customers, address_city = _load_customers(day, customer_ids)
    model.customers.update(customers)
    model.customers = dict(sorted(model.customers.items()))
    model.address_city.update(address_city)
    _build_pools(day, model)


def popularity(day: DayContext, model: SalesModel, product_id: int, launched: date) -> float:
    """Stable popularity of a product (log-normal, so a few products sell much more than the rest)."""
    weight = model.popularity.get(product_id)
    if weight is None:
        weight = day.streams.stream(launched, f"sales.popularity.{product_id}").lognormvariate(0, 1.1)
        model.popularity[product_id] = weight
    return weight


def lognormal_mean(sigma: float) -> float:
    return math.exp(sigma * sigma / 2)
