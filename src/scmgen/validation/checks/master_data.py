"""Master-data rules (1B §7, V-MD-*). Each check returns the violating rows; empty = the rule holds.

Not implemented here yet: V-MD-06 (approvers on documents, step 3.7).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import re

from scmgen.domain.identifiers import SYNTHETIC_PAN_PREFIX, is_valid_gstin
from scmgen.generation.master.codes import CODE_PATTERNS
from scmgen.generation.packs import GST_SLABS
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-MD-01", requires_master_data=True)
def codes_follow_patterns(ctx: ValidationContext) -> Rows:
    """Generated codes follow the generator's formats (generation/master/codes.py); company_code is the
    workspace code (V-MD-04); pack and seed codes are fixed by their YAML."""
    found: Rows = []
    for (table, column), pattern in CODE_PATTERNS.items():
        compiled = re.compile(pattern)
        for (value,) in ctx.conn.exec_driver_sql(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
            if not compiled.match(value):
                found.append({"table": table, "column": column, "value": value, "expected": pattern})
    return found


@check("V-MD-02", requires_master_data=True)
def gstins_valid_and_synthetic(ctx: ValidationContext) -> Rows:
    """Valid GSTIN check digits; company GSTINs embed the company PAN; every PAN starts with ZZX (D36)."""
    found: Rows = []
    company_pans = {r["company_id"]: r["pan"] for r in ctx.rows("SELECT company_id, pan FROM companies")}
    parties = [("company", r["company_id"], None, r["pan"]) for r in ctx.rows("SELECT company_id, pan FROM companies")]
    for r in ctx.rows("SELECT company_gst_registration_id AS id, company_id, gstin FROM company_gst_registrations"):
        parties.append(("company_gst_registration", r["id"], r["gstin"], None))
        if r["gstin"][2:12] != company_pans.get(r["company_id"]):
            found.append({"kind": "company_gst_registration", "id": r["id"], "gstin": r["gstin"],
                          "problem": "does not embed the company PAN"})
    for table, key in (("customers", "customer_id"), ("suppliers", "supplier_id")):
        for r in ctx.rows(f"SELECT {key} AS id, gstin, pan FROM {table} WHERE gstin IS NOT NULL OR pan IS NOT NULL"):
            parties.append((table[:-1], r["id"], r["gstin"], r["pan"]))
    for kind, key, gstin, pan in parties:
        if gstin is not None and not is_valid_gstin(gstin):
            found.append({"kind": kind, "id": key, "gstin": gstin, "problem": "invalid GSTIN check character"})
        for value, label in ((pan, "PAN"), (gstin[2:12] if gstin else None, "PAN inside GSTIN")):
            if value is not None and not value.startswith(SYNTHETIC_PAN_PREFIX):
                found.append({"kind": kind, "id": key, "value": value,
                              "problem": f"{label} lacks the synthetic prefix {SYNTHETIC_PAN_PREFIX}"})
    return found


_GSTIN_STATE_SQL = {
    "company_gst_registration": """
        SELECT r.company_gst_registration_id AS id, r.gstin, s.gst_state_code AS address_state
        FROM company_gst_registrations r JOIN states s ON s.state_id = r.state_id
        WHERE LEFT(r.gstin, 2) <> s.gst_state_code""",
    "customer": """
        SELECT c.customer_id AS id, c.gstin, s.gst_state_code AS address_state
        FROM customers c
        JOIN customer_addresses a ON a.customer_id = c.customer_id AND a.is_default_billing AND a.deleted_at IS NULL
        JOIN states s ON s.state_id = a.state_id
        WHERE c.gstin IS NOT NULL AND LEFT(c.gstin, 2) <> s.gst_state_code""",
    "supplier": """
        SELECT p.supplier_id AS id, p.gstin, s.gst_state_code AS address_state
        FROM suppliers p
        JOIN supplier_addresses a ON a.supplier_id = p.supplier_id AND a.address_type = 'REGISTERED'
             AND a.is_default AND a.deleted_at IS NULL
        JOIN states s ON s.state_id = a.state_id
        WHERE p.gstin IS NOT NULL AND LEFT(p.gstin, 2) <> s.gst_state_code""",
}


def _gstin_state_mismatches(ctx: ValidationContext, kinds: tuple[str, ...]) -> Rows:
    return [{"kind": kind, **row} for kind in kinds for row in ctx.rows(_GSTIN_STATE_SQL[kind])]


@check("V-MD-03", requires_master_data=True)
def gstin_state_matches(ctx: ValidationContext) -> Rows:
    """A GSTIN's first two digits are its state's GST code (registration state / billing / registered address)."""
    return _gstin_state_mismatches(ctx, ("company_gst_registration", "customer", "supplier"))


@check("V-MD-04", requires_master_data=True)
def single_company(ctx: ValidationContext) -> Rows:
    """One company per workspace, coded like the workspace; bank accounts in the base currency."""
    found: Rows = []
    companies = ctx.rows("SELECT company_id, company_code, base_currency_code FROM companies")
    if len(companies) != 1:
        found.append({"problem": f"{len(companies)} company rows (expected exactly 1)"})
    for company in companies:
        if ctx.workspace_code and company["company_code"] != ctx.workspace_code:
            found.append({"company_id": company["company_id"], "company_code": company["company_code"],
                          "problem": f"company_code differs from workspace code {ctx.workspace_code}"})
    found += [{"bank_account_id": r["bank_account_id"], "currency_code": r["currency_code"],
               "problem": "not the company's base currency"}
              for r in ctx.rows("SELECT b.bank_account_id, b.currency_code FROM bank_accounts b "
                                "WHERE b.currency_code NOT IN (SELECT base_currency_code FROM companies)")]
    return found


@check("V-MD-05", requires_master_data=True)
def employee_hierarchy(ctx: ValidationContext) -> Rows:
    """Exactly one employee without a manager; nobody manages themselves, directly or indirectly."""
    found: Rows = []
    tops = ctx.scalar("SELECT COUNT(*) FROM employees WHERE manager_employee_id IS NULL")
    if tops != 1:
        found.append({"problem": f"{tops} employees without a manager (expected exactly 1)"})
    found += [{"employee_id": r["employee_id"], "problem": "manager chain loops back to the employee"}
              for r in ctx.rows("""
        WITH RECURSIVE chain (employee_id, ancestor, depth) AS (
            SELECT employee_id, manager_employee_id, 1 FROM employees WHERE manager_employee_id IS NOT NULL
            UNION ALL
            SELECT c.employee_id, e.manager_employee_id, c.depth + 1
            FROM chain c JOIN employees e ON e.employee_id = c.ancestor
            WHERE e.manager_employee_id IS NOT NULL AND c.depth < 64 AND c.ancestor <> c.employee_id)
        SELECT DISTINCT employee_id FROM chain WHERE ancestor = employee_id""")]
    return found


@check("V-MD-07", requires_master_data=True)
def one_registration_per_warehouse_state(ctx: ValidationContext) -> Rows:
    """D3: every state with an active warehouse has exactly one ACTIVE GSTIN, and every ACTIVE GSTIN has a warehouse."""
    found = [{"state_id": r["state_id"], "active_registrations": r["n"],
              "problem": "warehouse state without exactly one ACTIVE GST registration"}
             for r in ctx.rows("""
        SELECT w.state_id, COUNT(DISTINCT r.company_gst_registration_id) AS n
        FROM warehouses w
        LEFT JOIN company_gst_registrations r ON r.state_id = w.state_id AND r.registration_status = 'ACTIVE'
        WHERE w.warehouse_status = 'ACTIVE' GROUP BY w.state_id HAVING n <> 1""")]
    found += [{"company_gst_registration_id": r["company_gst_registration_id"],
               "problem": "ACTIVE GST registration in a state without an active warehouse"}
              for r in ctx.rows("""
        SELECT r.company_gst_registration_id FROM company_gst_registrations r
        WHERE r.registration_status = 'ACTIVE' AND NOT EXISTS (
            SELECT 1 FROM warehouses w WHERE w.state_id = r.state_id AND w.warehouse_status = 'ACTIVE')""")]
    return found


@check("V-MD-08", requires_master_data=True)
def stores_replenished_by_active_dc(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT s.warehouse_id, s.replenishment_warehouse_id
        FROM warehouses s LEFT JOIN warehouses d ON d.warehouse_id = s.replenishment_warehouse_id
        WHERE s.warehouse_type = 'RETAIL_STORE'
          AND (d.warehouse_id IS NULL OR d.warehouse_type <> 'DISTRIBUTION_CENTRE' OR d.warehouse_status <> 'ACTIVE')""")


@check("V-MD-09", requires_master_data=True)
def warehouses_have_control_zones(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT w.warehouse_id, z.zone AS missing_zone
        FROM warehouses w
        CROSS JOIN (SELECT 'RECEIVING' AS zone UNION ALL SELECT 'QUARANTINE' UNION ALL SELECT 'DEFECTIVE') z
        WHERE w.warehouse_status = 'ACTIVE' AND NOT EXISTS (
            SELECT 1 FROM warehouse_locations l
            WHERE l.warehouse_id = w.warehouse_id AND l.zone = z.zone AND l.is_active AND l.deleted_at IS NULL)""")


@check("V-MD-10", requires_master_data=True)
def customers_have_default_addresses(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT c.customer_id,
               COALESCE(SUM(a.is_default_billing AND a.deleted_at IS NULL), 0) AS default_billing,
               COALESCE(SUM(a.is_default_shipping AND a.deleted_at IS NULL), 0) AS default_shipping
        FROM customers c LEFT JOIN customer_addresses a ON a.customer_id = c.customer_id
        WHERE c.deleted_at IS NULL
        GROUP BY c.customer_id HAVING default_billing <> 1 OR default_shipping <> 1""")


@check("V-MD-11", requires_master_data=True)
def addresses_match_city_list(ctx: ValidationContext) -> Rows:
    """City, state and PIN prefix agree with config/seed/cities.yaml; GSTIN state = billing / registered state."""
    prefixes = {(c["city"], c["state_code"]): c["pin_prefix"] for c in ctx.seed.cities}
    sources = {
        "company": "SELECT c.company_id AS id, c.registered_city AS city, s.state_code, c.registered_pincode AS pincode "
                   "FROM companies c JOIN states s ON s.state_id = c.registered_state_id",
        "warehouse": "SELECT w.warehouse_id AS id, w.city, s.state_code, w.pincode "
                     "FROM warehouses w JOIN states s ON s.state_id = w.state_id",
        "customer_address": "SELECT a.customer_address_id AS id, a.city, s.state_code, a.pincode "
                            "FROM customer_addresses a JOIN states s ON s.state_id = a.state_id",
        "supplier_address": "SELECT a.supplier_address_id AS id, a.city, s.state_code, a.pincode "
                            "FROM supplier_addresses a JOIN states s ON s.state_id = a.state_id",
    }
    found: Rows = []
    for kind, sql in sources.items():
        for r in ctx.rows(sql):
            prefix = prefixes.get((r["city"], r["state_code"]))
            if prefix is None:
                found.append({"kind": kind, **r, "problem": "city/state pair not in the city list"})
            elif not r["pincode"].startswith(prefix):
                found.append({"kind": kind, **r, "problem": f"PIN does not start with {prefix}"})
    return found + _gstin_state_mismatches(ctx, ("customer", "supplier"))


@check("V-MD-12", requires_master_data=True)
def acquisition_campaign_consistent(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT c.customer_id, c.acquisition_date, m.marketing_campaign_id, m.start_date, m.end_date
        FROM customers c JOIN marketing_campaigns m ON m.marketing_campaign_id = c.acquisition_marketing_campaign_id
        WHERE m.marketing_channel_id <> c.acquisition_marketing_channel_id
           OR c.acquisition_date NOT BETWEEN m.start_date AND m.end_date""")


@check("V-MD-13", requires_master_data=True)
def address_chains_linear(ctx: ValidationContext) -> Rows:
    """Superseded versions are soft-deleted, each version is superseded at most once, valid_from increases."""
    found: Rows = []
    for table, key, owner, previous in (("customer_addresses", "customer_address_id", "customer_id",
                                         "previous_customer_address_id"),
                                        ("supplier_addresses", "supplier_address_id", "supplier_id",
                                         "previous_supplier_address_id")):
        found += [{"table": table, **r} for r in ctx.rows(f"""
            SELECT n.{key} AS id, n.{previous} AS previous_id,
                   CASE WHEN o.{owner} <> n.{owner} THEN 'previous version belongs to another party'
                        WHEN o.deleted_at IS NULL THEN 'superseded version is still live'
                        ELSE 'valid_from does not increase along the chain' END AS problem
            FROM {table} n JOIN {table} o ON o.{key} = n.{previous}
            WHERE o.{owner} <> n.{owner} OR o.deleted_at IS NULL OR n.valid_from <= o.valid_from""")]
        found += [{"table": table, "id": r["previous_id"], "problem": f"superseded by {r['n']} versions"}
                  for r in ctx.rows(f"SELECT {previous} AS previous_id, COUNT(*) AS n FROM {table} "
                                    f"WHERE {previous} IS NOT NULL GROUP BY {previous} HAVING n > 1")]
    return found


@check("V-MD-14", requires_master_data=True)
def msme_terms_within_45_days(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT s.supplier_id, s.msme_category, t.term_code, t.due_days
        FROM suppliers s JOIN payment_terms t ON t.payment_term_id = s.payment_term_id
        WHERE s.msme_category IS NOT NULL AND t.due_days > 45""")


@check("V-MD-15", requires_master_data=True)
def products_have_goods_suppliers(ctx: ValidationContext) -> Rows:
    """Only GOODS/BOTH suppliers supply products; every active product has >= 2 active approved suppliers.

    The "products of the supplier's packs" part is enforced by generation: suppliers record no pack.
    """
    found = [{**r, "problem": "SERVICES supplier linked to a product"} for r in ctx.rows("""
        SELECT sp.supplier_product_id, sp.supplier_id, sp.product_id
        FROM supplier_products sp JOIN suppliers s ON s.supplier_id = sp.supplier_id
        WHERE s.supplier_type = 'SERVICES'""")]
    found += [{**r, "problem": "fewer than 2 active approved suppliers"} for r in ctx.rows("""
        SELECT p.product_id, COUNT(sp.supplier_product_id) AS suppliers
        FROM products p
        LEFT JOIN supplier_products sp ON sp.product_id = p.product_id AND sp.supplier_product_status = 'ACTIVE'
             AND sp.deleted_at IS NULL AND sp.approved_date IS NOT NULL
        LEFT JOIN suppliers s ON s.supplier_id = sp.supplier_id AND s.supplier_status = 'ACTIVE'
        WHERE p.product_status = 'ACTIVE' AND p.deleted_at IS NULL
        GROUP BY p.product_id HAVING COUNT(s.supplier_id) < 2""")]
    return found


@check("V-MD-16", requires_master_data=True)
def category_tree_consistent(ctx: ValidationContext) -> Rows:
    found = [{**r, "problem": "level is not parent level + 1"} for r in ctx.rows("""
        SELECT c.product_category_id, c.category_level, p.category_level AS parent_level
        FROM product_categories c JOIN product_categories p ON p.product_category_id = c.parent_category_id
        WHERE c.category_level <> p.category_level + 1""")]
    found += [{**r, "problem": "is_leaf disagrees with having children"} for r in ctx.rows("""
        SELECT c.product_category_id, c.is_leaf,
               EXISTS (SELECT 1 FROM product_categories k WHERE k.parent_category_id = c.product_category_id) AS has_children
        FROM product_categories c
        HAVING is_leaf = has_children""")]
    found += [{**r, "problem": "product on a non-leaf category"} for r in ctx.rows("""
        SELECT p.product_id, p.product_category_id FROM products p
        JOIN product_categories c ON c.product_category_id = p.product_category_id WHERE NOT c.is_leaf""")]
    return found


@check("V-MD-17", requires_master_data=True)
def style_variants_consistent(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT style_code, COUNT(DISTINCT product_category_id) AS categories, COUNT(DISTINCT product_brand_id) AS brands,
               COUNT(DISTINCT hsn_code) AS hsn_codes
        FROM products WHERE style_code IS NOT NULL
        GROUP BY style_code HAVING categories > 1 OR brands > 1 OR hsn_codes > 1""")


@check("V-MD-18", requires_master_data=True)
def list_price_within_mrp(ctx: ValidationContext) -> Rows:
    """list price + GST never exceeds MRP, at any rate the product has had (Legal Metrology)."""
    return ctx.rows("""
        SELECT p.product_id, p.list_price, p.mrp, MAX(r.gst_rate) AS highest_rate
        FROM products p JOIN product_tax_rates r ON r.product_id = p.product_id
        WHERE p.mrp IS NOT NULL
        GROUP BY p.product_id, p.list_price, p.mrp
        HAVING p.list_price * (1 + MAX(r.gst_rate) / 100) > p.mrp""")


@check("V-MD-19", requires_master_data=True)
def one_rate_per_hsn(ctx: ValidationContext) -> Rows:
    """Any two overlapping rate periods of products with the same HSN overlap at the later start date,
    so it is enough to compare the rates in force at each period start."""
    return ctx.rows("""
        WITH points AS (
            SELECT DISTINCT p.hsn_code, r.effective_from AS on_date
            FROM product_tax_rates r JOIN products p ON p.product_id = r.product_id)
        SELECT pt.hsn_code, pt.on_date, COUNT(DISTINCT r.gst_rate) AS rates
        FROM points pt
        JOIN products p ON p.hsn_code = pt.hsn_code
        JOIN product_tax_rates r ON r.product_id = p.product_id
             AND r.effective_from <= pt.on_date AND (r.effective_to IS NULL OR r.effective_to >= pt.on_date)
        GROUP BY pt.hsn_code, pt.on_date HAVING rates > 1""")


@check("V-MD-20", requires_master_data=True)
def rates_from_the_pack(ctx: ValidationContext) -> Rows:
    """Every product GST rate is a GST slab and one of its pack category's rates (before / after D17)."""
    found: Rows = []
    leaves = {code: {leaf.code: leaf for leaf in pack.leaves} for code, pack in ctx.packs.items()}
    for r in ctx.rows("""
            SELECT t.product_tax_rate_id, t.product_id, t.gst_rate, c.category_code, b.industry_pack_code
            FROM product_tax_rates t JOIN products p ON p.product_id = t.product_id
            JOIN product_categories c ON c.product_category_id = p.product_category_id
            JOIN business_units b ON b.business_unit_id = p.business_unit_id"""):
        leaf = leaves.get(r["industry_pack_code"], {}).get(r["category_code"])
        if r["gst_rate"] not in GST_SLABS:
            found.append({**r, "problem": "not a GST slab"})
        elif leaf is None:
            found.append({**r, "problem": "category is not a product category of the business unit's pack"})
        elif Decimal(r["gst_rate"]) not in (leaf.gst_before, leaf.gst_after):
            found.append({**r, "problem": f"pack rates are {leaf.gst_before} / {leaf.gst_after}"})
    return found


@check("V-MD-21", requires_master_data=True)
def tax_periods_contiguous(ctx: ValidationContext) -> Rows:
    """Rate periods start at launch, follow each other without gaps or overlaps, and the last one is open."""
    periods: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.rows("""
            SELECT p.product_id, p.launch_date, t.effective_from, t.effective_to
            FROM products p LEFT JOIN product_tax_rates t ON t.product_id = p.product_id
            ORDER BY p.product_id, t.effective_from"""):
        periods[r["product_id"]].append(r)
    found: Rows = []
    for product_id, rows in periods.items():
        if rows[0]["effective_from"] is None:
            found.append({"product_id": product_id, "problem": "no GST rate"})
            continue
        if rows[0]["effective_from"] != rows[0]["launch_date"]:
            found.append({"product_id": product_id, "problem": "first rate does not start at launch_date"})
        for earlier, later in zip(rows, rows[1:]):
            if earlier["effective_to"] is None or later["effective_from"] != earlier["effective_to"] + timedelta(days=1):
                found.append({"product_id": product_id, "from": later["effective_from"],
                              "problem": "gap or overlap between rate periods"})
        if rows[-1]["effective_to"] is not None:
            found.append({"product_id": product_id, "problem": "no current (open-ended) rate"})
    return found


@check("V-MD-22", requires_master_data=True)
def campaigns_on_campaign_channels(ctx: ValidationContext) -> Rows:
    return ctx.rows("""
        SELECT m.marketing_campaign_id, c.channel_code, d.department_code AS owner_department,
               CASE WHEN NOT c.supports_campaigns THEN 'channel does not support campaigns'
                    ELSE 'owner is not in Marketing' END AS problem
        FROM marketing_campaigns m
        JOIN marketing_channels c ON c.marketing_channel_id = m.marketing_channel_id
        JOIN employees e ON e.employee_id = m.owner_employee_id
        JOIN departments d ON d.department_id = e.department_id
        WHERE NOT c.supports_campaigns OR d.department_code <> 'MKT'""")
