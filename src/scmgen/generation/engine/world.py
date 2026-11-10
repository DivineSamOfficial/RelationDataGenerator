"""The engine's in-memory view of a workspace, loaded from the database when a run starts (D66).

Everything here is derived from rows already committed, so a run started from any committed day sees
the same state an uninterrupted run would have had at that point; the day steps keep it in step with
every row they write. Nothing lives only in memory (no hidden event queue).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.generation.config import GenerationConfig
from scmgen.generation.master.catalogue import CatalogueCounters
from scmgen.generation.packs import Category, IndustryPack
from scmgen.generation.seed import SeedData


@dataclass
class World:
    config: GenerationConfig
    seed: SeedData
    packs: list[IndustryPack]
    workspace_code: str
    company_name: str
    refs: dict[str, Any]                                   # the master-data lookups builders use
    leaf_of_category: dict[str, tuple[IndustryPack, Category]]
    active_products: dict[str, list[tuple[int, date]]]     # pack code → [(product_id, launch_date)] ACTIVE
    finance_closer: int                                    # employee who closes accounting periods
    campaign_numbers: dict[int, int] = field(default_factory=dict)   # last campaign number per start year
    first_day: date | None = None                          # first simulated business day of the workspace

    @property
    def packs_by_code(self) -> dict[str, IndustryPack]:
        return {p.code: p for p in self.packs}


def _pairs(conn: Connection, sql: str) -> dict[Any, Any]:
    return dict(conn.execute(text(sql)).all())


def load_world(conn: Connection, config: GenerationConfig, seed: SeedData, packs: list[IndustryPack],
               workspace_code: str, company_name: str) -> World:
    refs: dict[str, Any] = {
        "business_unit": _pairs(conn, "SELECT industry_pack_code, business_unit_id FROM business_units"),
        "brand": _pairs(conn, "SELECT brand_code, product_brand_id FROM product_brands"),
        "category": _pairs(conn, "SELECT category_code, product_category_id FROM product_categories"),
        "uom": _pairs(conn, "SELECT uom_code, unit_of_measure_id FROM product_units_of_measure"),
        "marketing_channel": _pairs(conn, "SELECT channel_code, marketing_channel_id FROM marketing_channels"),
    }
    bu_pack = {bu_id: code for code, bu_id in refs["business_unit"].items()}

    # Catalogue numbering and names per pack, so launches continue the day-0 sequences.
    counters: dict[str, CatalogueCounters] = {p.code: CatalogueCounters() for p in packs}
    active: dict[str, list[tuple[int, date]]] = defaultdict(list)
    for product_id, bu_id, sku, style, name, status, launched in conn.execute(text(
            "SELECT product_id, business_unit_id, sku, style_code, product_name, product_status, launch_date "
            "FROM products ORDER BY product_id")):
        pack_code = bu_pack[bu_id]
        counter = counters[pack_code]
        counter.sku = max(counter.sku, int(re.search(r"([0-9]+)$", sku).group(1)))
        if style:
            counter.style = max(counter.style, int(re.search(r"([0-9]+)$", style).group(1)))
        counter.add_name(name)
        if status == "ACTIVE":
            active[pack_code].append((product_id, launched))
    refs["catalogue_counters"] = counters

    # Goods suppliers per pack = suppliers approved for that pack's products (suppliers record no pack).
    goods: dict[str, set[int]] = defaultdict(set)
    for supplier_id, bu_id in conn.execute(text(
            "SELECT DISTINCT sp.supplier_id, p.business_unit_id FROM supplier_products sp "
            "JOIN products p ON p.product_id = sp.product_id JOIN suppliers s ON s.supplier_id = sp.supplier_id "
            "WHERE s.supplier_type IN ('GOODS', 'BOTH') AND s.supplier_status = 'ACTIVE'")):
        goods[bu_pack[bu_id]].add(supplier_id)
    refs["goods_suppliers"] = {code: sorted(ids) for code, ids in goods.items()}
    refs["supplier"] = {
        supplier_id: {"rating": Decimal(rating), "onboarded": onboarded, "code": code}
        for supplier_id, rating, onboarded, code in conn.execute(text(
            "SELECT supplier_id, supplier_rating, onboarded_date, supplier_code FROM suppliers"))}
    refs["employees_by_department"] = defaultdict(list)
    for department, employee_id in conn.execute(text(
            "SELECT d.department_code, e.employee_id FROM employees e JOIN departments d USING (department_id) "
            "WHERE e.employment_status = 'ACTIVE' ORDER BY e.employee_id")):
        refs["employees_by_department"][department].append(employee_id)
    closer = conn.execute(text(
        "SELECT e.employee_id FROM employees e JOIN departments d USING (department_id) "
        "WHERE d.department_code = 'FIN' AND e.employment_status = 'ACTIVE' "
        "ORDER BY FIELD(e.job_level, 'HEAD', 'MANAGER', 'LEAD', 'STAFF'), e.employee_id LIMIT 1")).scalar_one()
    campaign_numbers: dict[int, int] = {}
    for (code,) in conn.execute(text("SELECT campaign_code FROM marketing_campaigns")):
        year, number = int(code[4:8]), int(code[9:])
        campaign_numbers[year] = max(campaign_numbers.get(year, 0), number)
    setup_day = conn.execute(text(
        "SELECT MIN(sim_start_date) FROM generation_runs WHERE run_status = 'COMPLETED'")).scalar()
    first_day = setup_day + timedelta(days=1) if setup_day else None     # day 0 = master data (step 3.5)
    leaves = {leaf.code: (pack, leaf) for pack in packs for leaf in pack.leaves}
    return World(config=config, seed=seed, packs=packs, workspace_code=workspace_code, company_name=company_name,
                 refs=refs, leaf_of_category=leaves, active_products=active, finance_closer=closer,
                 campaign_numbers=campaign_numbers, first_day=first_day)
