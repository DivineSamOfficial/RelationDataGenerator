# Phase 1 – Part 1B: Master Data and Accounting Master Tables

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9). Baseline for Phase 2
**Last updated:** 2026-11-08 (amended by Part 1C: D25 currency)
**Builds on:** Part 1A (`phase1_part1a_business_and_entities.md`): decisions D1–D14, entity inventory §5, addenda A1/A2

---

## 0. Scope

This part defines every column, key, constraint and relationship for **31 tables**, and how each one is populated:

| Group | Tables | Count |
|---|---|---|
| Geography & company | `states`, `companies`, `company_gst_registrations` | 3 |
| Organisation | `business_units`, `departments`, `employees` | 3 |
| Locations | `warehouses`, `warehouse_locations` | 2 |
| Customers | `customers`, `customer_addresses` | 2 |
| Suppliers | `suppliers`, `supplier_addresses`, `supplier_products` | 3 |
| Product catalogue | `product_categories`, `product_brands`, `product_units_of_measure`, `products`, `product_tax_rates` | 5 |
| Commercial reference | `payment_methods`, `payment_terms`, `shipping_carriers`, `sales_channels`, `marketing_channels`, `marketing_campaigns` | 6 |
| Finance master | `expense_categories`, `bank_accounts` | 2 |
| Accounting master | `account_types`, `chart_of_accounts`, `accounting_periods`, `cost_centers`, `tax_accounts` | 5 |

That's the 23 master tables and 5 accounting master tables from Part 1A §5 A–B, plus the 3 attribution tables added in Addendum A1 (`sales_channels`, `marketing_channels`, `marketing_campaigns`). **No tables are added or removed.** The total stays at 92. All tables live in each workspace's clean schema `scm_<code>_oltp` (D13).

The change-management rule is met: this part adds columns that 1A didn't list. They're summarised in §1.4. No relationship from 1A is changed.

---

## 1. Decisions Made in This Part

### 1.1 New decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| D15 | FK actions and deletion | Every FK is `ON DELETE RESTRICT ON UPDATE RESTRICT`. Master data is never hard-deleted. It's deactivated (status) or soft-deleted (`deleted_at`) | Cascading deletes would silently destroy transaction history. MySQL also forbids referential actions on columns used in `CHECK` constraints, so RESTRICT keeps all CHECKs legal |
| D16 | Account determination (how posting logic finds GL accounts) | No account codes hardcoded in Python. Accounts are found through master data: `chart_of_accounts.system_role` for singleton accounts (AR control, inventory, GRNI…), `business_units` for revenue / sales returns / COGS, `tax_accounts` per GSTIN for GST, `expense_categories.gl_account_id`, `bank_accounts.gl_account_id` | This is how real ERPs work ("account determination"). The COA can be renumbered in config without code changes, and P&L by business unit falls out naturally |
| D17 | GST rates | Effective-dated **per product** in `product_tax_rates` (one row per product per validity period). Default rate schedule comes from the industry pack and includes the **22 Sep 2025 GST rate rationalisation** (most 12% → 5%, most 28% → 18%, 40% for select goods), switchable off in config | Gives real effective-dated master data (SCD practice) and invoices from both sides of a real rate change. Transaction lines still snapshot the rate (1A §3) |
| D18 | Units of measure | One **base UoM per product**, used on every transaction in v1 (no purchase-pack ↔ base conversions) | Conversions add a lot of quantity-rounding complexity and little analytics value. `pack_size` is kept as information only |
| D19 | Circular references | Avoided. No `head_employee_id` on `departments` and no `manager_employee_id` on `warehouses`. Hierarchy and roles come from `employees.manager_employee_id`, `job_level` and the employee's department/warehouse | A department → employee → department cycle forces two-pass inserts and nullable FKs, and makes drop/rebuild and DuckDB load order fragile |
| D20 | Surrogate key assignment | The generator assigns surrogate IDs itself, deterministically, from the seed and insert order. `AUTO_INCREMENT` stays on the column for manual inserts | Parent and child rows can be built in memory and batch-inserted without a round trip to read back IDs. Same seed → same IDs, so Rebuild (D14) is reproducible down to the keys |
| D21 | Audit timestamps | `created_at` / `updated_at` are supplied by the generator from the **simulated clock**. Columns have `DEFAULT CURRENT_TIMESTAMP(6)` as a safety net but **no `ON UPDATE CURRENT_TIMESTAMP`** | `ON UPDATE` would overwrite simulated time with wall-clock time and break watermarks. Provisional, to be confirmed with Q4 in Part 1G |
| D22 | Opening balances | Not stored on master tables (e.g. no `opening_balance` on `bank_accounts`). They're posted as an **opening journal entry** on the first day of the simulated history (Part 1E) | Keeps `journal_entry_lines` as the only accounting source of truth |
| D23 | Interpretation of D2 (80/20 mix) | The B2B/B2C ratio applies to **net sales value**. Customer *counts* are configured separately: B2C customers usually outnumber B2B customers even when B2B dominates revenue | Matches how distributors actually look: few large B2B accounts, many small consumers |
| D24 | Fictional contact data | Every generated email and website uses the reserved `.example` top-level domain (RFC 2606), e.g. `accounts@kaverinexus.example`. Bank names and IFSC bank prefixes are invented | Guarantees generated emails and URLs can never belong to a real party |

### 1.2 Deferred (out of scope for v1, can be added later without breaking existing tables)

| # | Item | Note |
|---|---|---|
| R23 | GST compensation cess | No cess column in v1. Only matters for a few goods (aerated drinks, tobacco, motor vehicles), which the packs don't include |
| R24 | TDS (tax deducted at source) on vendor payments (sections 194C/194I/194J) | Realistic for rent, professional fees and contractors, but adds a liability flow and its own returns. Candidate for a later addendum |
| R25 | Multi-GSTIN B2B customers | One GSTIN per customer row. A chain with branches in several states is modelled as several customer rows |
| R26 | Imports, multi-currency transactions, reverse-charge mechanism (RCM) | All suppliers are domestic. Every amount is in the workspace's single base currency (default INR, configurable per D25 in Part 1C) |

### 1.3 New open questions

| # | Question | Part |
|---|---|---|
| Q6 | Product list-price history: overwrite `products.list_price` in place (current design, order lines snapshot price) or add a `product_price_history` table for "price on date X" analytics | 1D |
| Q7 | Stock transfers between warehouses under **different GSTINs** count as taxable supplies between "distinct persons" under GST (they need a tax invoice and IGST, not just a delivery challan). How transfers are documented and taxed | 1C |
| Q8 | Reorder level/quantity per warehouse (override table or columns on `inventory`) vs the product-level defaults defined here | 1C |

### 1.4 Columns added beyond the Part 1A descriptions

| Table | Added | Reason |
|---|---|---|
| `business_units` | `revenue_account_id`, `sales_returns_account_id`, `cogs_account_id` | D16 |
| `chart_of_accounts` | `system_role`, `normal_balance` (overrides the type's default for contra accounts), `requires_cost_center` | D16; contra accounts such as Sales Returns and Accumulated Depreciation |
| `customers`, `suppliers` | `gst_registration_type` (`REGISTERED` / `UNREGISTERED` / `COMPOSITION` / `CONSUMER`) | Decides tax-invoice content and the GSTR-1 section (B2B vs B2CL vs B2CS) |
| `suppliers` | `msme_category` | MSME vendors must be paid within 45 days. Gives AP ageing a real business rule |
| `shipping_carriers` | `supplier_id` | Carriers bill freight. Their invoices flow through the normal vendor/AP process |
| `payment_methods` | `settlement_lag_days`, `gateway_fee_percent`, `min_amount`, `max_amount` | Drive bank value dates, gateway charges and legal limits (e.g. RTGS minimum, cash receipt limit) |
| `product_categories` | `return_window_days` | Return eligibility varies by category (e.g. 7 days electronics, 30 days apparel) |
| `products` | `mrp`, `is_returnable`, `warranty_months`, variant columns | Indian MRP rule, non-returnable items, warranty returns, fashion variants |
| `warehouses` | `latitude`, `longitude` | Nearest-warehouse allocation and distance-based transit times |

---

## 2. Conventions Used in Every Table Below

### 2.1 Standard columns (not repeated in each table)

| Column | Type | Null | Notes |
|---|---|---|---|
| `created_at` | `DATETIME(6)` | NO | Simulated clock (D21) |
| `updated_at` | `DATETIME(6)` | NO | Simulated clock. `= created_at` on insert; set explicitly on every update |
| `deleted_at` | `DATETIME(6)` | YES | **Only on tables marked ✱ (soft-deletable).** `NULL` = live row |

Indexes on `updated_at` are added in Phase 2 for every table the loader reads incrementally (Part 1G, Q5).

### 2.2 Type and naming rules (from 1A §3, applied)

- PK `<entity>_id`. `SMALLINT UNSIGNED` for small reference tables that never grow much; `INT UNSIGNED` for medium ones; `BIGINT UNSIGNED` for anything that grows with the simulation (customers, products, addresses…). An FK column always has exactly the same type as the PK it references.
- FKs whose name doesn't match the target table carry a role prefix: `base_unit_of_measure_id`, `revenue_account_id`, `manager_employee_id`.
- Booleans are `BOOLEAN` (`TINYINT(1)`), `NOT NULL` with a default.
- Status / type columns are `VARCHAR(30)` + `CHECK (col IN (...))`.
- Codes are upper-case, fixed patterns, `UNIQUE` (e.g. `CUST-000123`).

### 2.3 Reusable format checks (CHECK with `REGEXP`)

| Name | Pattern | Example (fictional) |
|---|---|---|
| GSTIN | `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$` | `29AAKCK4821M1Z7` |
| PAN | `^[A-Z]{5}[0-9]{4}[A-Z]$` | `AAKCK4821M` |
| GSTIN ↔ PAN | `SUBSTRING(gstin, 3, 10) = pan` (same-row CHECK when both present) | |
| CIN | `^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$` | `U51909KA2019PTC128734` |
| IFSC | `^[A-Z]{4}0[A-Z0-9]{6}$` | `DCBK0004127` |
| PIN code | `^[1-9][0-9]{5}$` | `560038` |
| Mobile | `^[6-9][0-9]{9}$` (10 digits, no +91) | `9845012345` |
| HSN | `^[0-9]{4}([0-9]{2}){0,2}$` (4, 6 or 8 digits) | `85044030` |

GSTINs are generated with a valid structure and a correctly computed check digit, from randomly generated fictional PANs. They are never derived from real entities. **Every generated PAN (company, customer, supplier) starts with the fixed synthetic prefix `ZZX`** (config `synthetic_identifiers.pan_prefix`), so the GSTINs built from them share it at positions 3–5 (D36, Part 1C). This makes synthetic identifiers easy to recognise and lowers, but doesn't eliminate, the chance of matching a real registration. The examples in the table above predate D36; real generated values look like `29ZZXCK4821M1Z7`. MySQL's support for `REGEXP` inside `CHECK` will be confirmed by a test in Phase 2, not assumed.

### 2.4 Two constraint techniques used repeatedly

**a) Composite FK to enforce "same parent" consistency.** Where two FKs on a row must agree (a warehouse's state must equal its GST registration's state), the parent gets an extra `UNIQUE (pk, attribute)` and the child references both columns. The database then enforces the rule without triggers. If any column of a composite FK is NULL, MySQL skips the check, so it also works for nullable self-references.

**b) Generated column + UNIQUE for "at most one X".** E.g. at most one default billing address per customer:
`default_billing_key BIGINT UNSIGNED GENERATED ALWAYS AS (IF(is_default_billing AND deleted_at IS NULL, customer_id, NULL)) STORED` + `UNIQUE (default_billing_key)`. NULLs don't collide in a UNIQUE index, so only live default rows compete.

### 2.5 Self-referencing tables

`employees`, `chart_of_accounts`, `product_categories`, `cost_centers`, `warehouses`, `customer_addresses`, `supplier_addresses`. InnoDB checks FKs row by row, even within one multi-row `INSERT`, so the generator **orders rows parents-first** (by hierarchy level) within each batch.

---

## 3. Population Overview

### 3.1 Population order (FK dependency levels)

Each level only references tables in earlier levels (or itself). Phase 3 inserts in this order. Rebuild drops in reverse.

| Level | Tables |
|---|---|
| 0 | `states`, `account_types`, `departments`, `product_units_of_measure`, `product_brands`, `payment_methods`, `payment_terms`, `sales_channels`, `marketing_channels` |
| 1 | `companies`, `chart_of_accounts` |
| 2 | `company_gst_registrations`, `business_units`, `expense_categories` |
| 3 | `warehouses`, `tax_accounts`, `product_categories`, `suppliers` |
| 4 | `warehouse_locations`, `bank_accounts`, `cost_centers`, `products`, `supplier_addresses`, `shipping_carriers` |
| 5 | `employees`, `product_tax_rates`, `supplier_products` |
| 6 | `accounting_periods`, `marketing_campaigns` |
| 7 | `customers` |
| 8 | `customer_addresses` |

`chart_of_accounts` is built in one pass that contains both the static skeleton (seed file) and the accounts derived from setup inputs (one revenue/returns/COGS account per business unit, one GL account per bank account, six GST accounts per GSTIN). Setup inputs are known before any insert, so no back-filling is needed.

### 3.2 Population sources

| Source | Meaning | Tables |
|---|---|---|
| **Seed** | Static reference data shipped with the project as YAML (`config/seed/`), identical in every workspace | `states`, `account_types`, `departments`, `product_units_of_measure`, `payment_methods`, `payment_terms`, `sales_channels`, `marketing_channels`, `expense_categories`, COA skeleton |
| **Setup** | Inputs from the Control Panel / CLI: company name, HQ state, industries, warehouse cities, store count | `companies`, `company_gst_registrations`, `business_units`, `warehouses`, `bank_accounts` |
| **Pack** | Industry pack YAML (D11) | `product_categories`, `product_brands`, `products`, `product_tax_rates`, campaign calendar |
| **Faker** | Realistic attributes (names, streets, phones) with the `en_IN` locale, seeded | `customers`, `customer_addresses`, `suppliers`, `supplier_addresses`, `employees`, `shipping_carriers` (names) |
| **Derived** | Computed from other master data by rules | `cost_centers`, `tax_accounts`, derived COA accounts, `accounting_periods`, `warehouse_locations`, `supplier_products` |

### 3.3 How master data changes over time (Continue mode, D14)

Master data isn't frozen after the first run. Realistic change is what makes CDC and SCD exercises meaningful (Q5 finalises the load strategy per table).

| Behaviour | Tables |
|---|---|
| **Static** after workspace creation | `states`, `account_types`, `departments`, `product_units_of_measure`, `payment_methods`, `payment_terms`, `sales_channels`, `marketing_channels`, `companies`, `company_gst_registrations`, `business_units`, `warehouses`, `warehouse_locations`, `expense_categories`, `bank_accounts`, `chart_of_accounts`, `cost_centers`, `tax_accounts`, `shipping_carriers`, `product_brands` |
| **Grows** (new rows only) | `product_tax_rates` (new period on a rate change), `marketing_campaigns` (new each season) |
| **Grows and mutates** | `customers` (new customers, status / credit-limit changes), `customer_addresses` (new versions, soft-delete old), `suppliers` (rating, status), `supplier_addresses`, `supplier_products` (reference cost, lead time, new approvals), `products` (new launches, price changes, discontinuation), `product_categories` (rare new leaves), `employees` (hires, exits, transfers), `accounting_periods` (new FY rows; FUTURE → OPEN → CLOSED), `marketing_campaigns` (PLANNED → ACTIVE → COMPLETED) |

### 3.4 Default volumes (Medium preset; Small ≈ ÷5, Large ≈ ×5 for scalable tables)

| Table | Medium | Scales? |
|---|---|---|
| `states` | 37 | No |
| `companies` / `company_gst_registrations` | 1 / 5 | No (setup) |
| `business_units` | 1–5 (one per chosen industry) | No |
| `departments` | 10 | No |
| `employees` | ~180 | Yes |
| `warehouses` | 8 (5 DCs + 3 stores) | No (setup) |
| `warehouse_locations` | ~2,200 (~400 per DC, ~60 per store) | Yes |
| `customers` | ~7,000 (≈1,200 B2B, ≈5,800 B2C) | Yes |
| `customer_addresses` | ~10,500 (plus versions over time) | Yes |
| `suppliers` | ~260 (≈200 goods, ≈60 services/both) | Yes |
| `supplier_addresses` | ~420 | Yes |
| `products` | ~2,000 | Yes |
| `product_tax_rates` | ~2,000 + revisions | Yes |
| `supplier_products` | ~6,000 (2–5 suppliers per product) | Yes |
| `product_categories` / `product_brands` | ~120 / ~60 | Pack |
| `product_units_of_measure` | 12 | No |
| `payment_methods` / `payment_terms` | 8 / 7 | No |
| `shipping_carriers` | 6 | No |
| `sales_channels` / `marketing_channels` | 7 / 11 | No |
| `marketing_campaigns` | ~8 per year | Pack |
| `expense_categories` | ~30 | No |
| `bank_accounts` | 3 bank + 8 petty cash | No |
| `account_types` | 6 | No |
| `chart_of_accounts` | ~130 | Derived |
| `accounting_periods` | 12 per FY in range | Derived |
| `cost_centers` | ~25 | Derived |
| `tax_accounts` | 30 (6 per GSTIN) | Derived |

---

## 4. Table Definitions

Column tables list business columns. The standard columns from §2.1 are always added. **NN** = NOT NULL.

### 4.1 Geography & Company

#### `states`
One row per Indian state / union territory, keyed to its GST state code.

| Column | Type | NN | Notes |
|---|---|---|---|
| `state_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `gst_state_code` | `CHAR(2)` | ✓ | UNIQUE. `29` = Karnataka, `27` = Maharashtra, `97` = Other Territory |
| `state_code` | `CHAR(2)` | ✓ | UNIQUE. ISO-style alpha code (`KA`, `MH`) |
| `state_name` | `VARCHAR(60)` | ✓ | UNIQUE |
| `is_union_territory` | `BOOLEAN` | ✓ | |
| `intra_state_tax_component` | `VARCHAR(5)` | ✓ | `SGST` or `UTGST`. UTs with a legislature (Delhi, Puducherry, J&K) use SGST; other UTs use UTGST |
| `region` | `VARCHAR(20)` | ✓ | `NORTH` / `SOUTH` / `EAST` / `WEST` / `CENTRAL` / `NORTH_EAST`. Used for transit-time and demand rules |

- **Constraints:** CHECK `gst_state_code` REGEXP `^[0-9]{2}$`; CHECK `intra_state_tax_component IN ('SGST','UTGST')`; CHECK `region IN (...)`; CHECK `(is_union_territory OR intra_state_tax_component = 'SGST')`.
- **Relationships:** Referenced by `companies`, `company_gst_registrations`, `warehouses`, `customer_addresses`, `supplier_addresses`.
- **Populated:** Seed. The current list of state/UT GST codes (36 states/UTs plus `97` Other Territory). Retired codes such as `28` (old Andhra Pradesh) are excluded. Static.

#### `companies`
The legal entity that owns the workspace. Exactly one row per workspace (D13). The table is kept so the company's identity is data, not config.

| Column | Type | NN | Notes |
|---|---|---|---|
| `company_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `company_code` | `VARCHAR(10)` | ✓ | UNIQUE. Equals the workspace code (`abc`, `kaveri`) |
| `legal_name` | `VARCHAR(200)` | ✓ | "Kaveri Nexus Distribution Pvt. Ltd." |
| `trade_name` | `VARCHAR(200)` | ✓ | "Kaveri Nexus" |
| `cin` | `CHAR(21)` | ✓ | UNIQUE. Fictional corporate identity number |
| `pan` | `CHAR(10)` | ✓ | UNIQUE. 4th character `C` (company) |
| `registered_state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states` (HQ state) |
| `registered_address_line1` | `VARCHAR(200)` | ✓ | |
| `registered_address_line2` | `VARCHAR(200)` | | |
| `registered_city` | `VARCHAR(80)` | ✓ | |
| `registered_pincode` | `CHAR(6)` | ✓ | |
| `incorporation_date` | `DATE` | ✓ | Before the simulated history starts |
| `base_currency_code` | `CHAR(3)` | ✓ | Default `INR`. Workspace setting (D25, Part 1C); one currency per workspace |
| `fy_start_month` | `TINYINT UNSIGNED` | ✓ | Default 4 (April, D7) |
| `email` | `VARCHAR(150)` | ✓ | `.example` domain (D24) |
| `phone` | `VARCHAR(10)` | ✓ | |
| `website` | `VARCHAR(150)` | | `https://kaverinexus.example` |

- **Constraints:** CIN, PAN, PIN, mobile format CHECKs (§2.3); CHECK `SUBSTRING(pan,4,1) = 'C'`; CHECK `fy_start_month BETWEEN 1 AND 12`; CHECK `base_currency_code REGEXP '^[A-Z]{3}$'` (amended by D25, Part 1C).
- **Relationships:** 1 : N `company_gst_registrations`.
- **Populated:** Setup (company name, HQ state); CIN and PAN generated; HQ address via Faker. Static.

#### `company_gst_registrations`
One GSTIN per state where the company has a DC or store (D3).

| Column | Type | NN | Notes |
|---|---|---|---|
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `company_id` | `SMALLINT UNSIGNED` | ✓ | FK → `companies` |
| `state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states` |
| `gstin` | `CHAR(15)` | ✓ | UNIQUE |
| `registration_date` | `DATE` | ✓ | On or before the first warehouse opening in that state |
| `is_principal_place` | `BOOLEAN` | ✓ | TRUE for the HQ state |
| `registration_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `SUSPENDED` / `CANCELLED` |
| `cancelled_date` | `DATE` | | |
| `principal_key` | generated `SMALLINT UNSIGNED` | | `IF(is_principal_place, company_id, NULL)` STORED |

- **Keys:** UNIQUE `(company_id, state_id)` (one per state, D3); UNIQUE `(company_gst_registration_id, state_id)` (target of the composite FK from `warehouses`, §2.4a); UNIQUE `(principal_key)` (one principal place).
- **Constraints:** GSTIN format; CHECK status list; CHECK `(registration_status = 'CANCELLED') = (cancelled_date IS NOT NULL)`.
- **Relationships:** N : 1 `companies`, `states`; 1 : N `warehouses`, `tax_accounts`. Later referenced by invoices, supplier invoices, tax transactions, GST returns and `document_sequences`.
- **Populated:** Derived from setup. One row per distinct warehouse state. The GSTIN is built as state code + company PAN + `1` + `Z` + computed check digit. Static.

### 4.2 Organisation

#### `business_units`
Product divisions, one per chosen industry pack (D11). Each owns its revenue, sales returns and COGS accounts (D16).

| Column | Type | NN | Notes |
|---|---|---|---|
| `business_unit_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `business_unit_code` | `VARCHAR(10)` | ✓ | UNIQUE (`ELEC`, `HOME`, `BEAUTY`) |
| `business_unit_name` | `VARCHAR(100)` | ✓ | UNIQUE |
| `industry_pack_code` | `VARCHAR(40)` | ✓ | UNIQUE. Key of `config/industries/<pack>.yaml` |
| `description` | `VARCHAR(255)` | | |
| `revenue_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts`, UNIQUE |
| `sales_returns_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts`, UNIQUE |
| `cogs_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts`, UNIQUE |
| `launched_date` | `DATE` | ✓ | |
| `is_active` | `BOOLEAN` | ✓ | Default TRUE |

- **Constraints:** Account-type correctness (revenue → REVENUE, COGS → COGS) is checked by validation V-AC-03 (it's a cross-table rule).
- **Relationships:** 1 : N `product_categories`, `products`, `marketing_campaigns`; 1 : 1 `cost_centers` (type BUSINESS_UNIT).
- **Populated:** Setup (selected industries); account links point to the COA accounts derived for each BU (§5). Static.

#### `departments`
Organisational departments (R18).

| Column | Type | NN | Notes |
|---|---|---|---|
| `department_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `department_code` | `VARCHAR(10)` | ✓ | UNIQUE |
| `department_name` | `VARCHAR(100)` | ✓ | UNIQUE |
| `is_active` | `BOOLEAN` | ✓ | |

- **Relationships:** 1 : N `employees`; 1 : 1 `cost_centers` (type DEPARTMENT).
- **Populated:** Seed. `MGMT` Management, `PUR` Purchasing, `WHO` Warehouse Operations, `LOG` Logistics, `SAL` Sales, `MKT` Marketing, `CS` Customer Service, `FIN` Finance & Accounts, `HR` HR & Admin, `IT` IT. Static.

#### `employees` ✱
Staff who create, approve and handle documents, and who are paid salaries.

| Column | Type | NN | Notes |
|---|---|---|---|
| `employee_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `employee_code` | `VARCHAR(12)` | ✓ | UNIQUE (`EMP-00042`) |
| `first_name` | `VARCHAR(60)` | ✓ | Faker `en_IN` |
| `last_name` | `VARCHAR(60)` | ✓ | |
| `email` | `VARCHAR(150)` | ✓ | UNIQUE, company `.example` domain |
| `phone` | `VARCHAR(10)` | ✓ | |
| `department_id` | `SMALLINT UNSIGNED` | ✓ | FK → `departments` |
| `warehouse_id` | `SMALLINT UNSIGNED` | | FK → `warehouses`. Work location; NULL = head office |
| `cost_center_id` | `SMALLINT UNSIGNED` | ✓ | FK → `cost_centers`. Where salary cost is charged |
| `manager_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (self). NULL only for the top executive |
| `job_title` | `VARCHAR(100)` | ✓ | "Purchase Executive", "Warehouse Manager – Bhiwandi" |
| `job_level` | `VARCHAR(30)` | ✓ | `EXECUTIVE` / `HEAD` / `MANAGER` / `LEAD` / `STAFF` |
| `approval_limit` | `DECIMAL(18,2)` | ✓ | Default 0. Max document value this person may approve (POs, expenses, refunds) |
| `monthly_gross_salary` | `DECIMAL(18,2)` | ✓ | Drives the salary expense (Part 1E) |
| `hire_date` | `DATE` | ✓ | |
| `termination_date` | `DATE` | | |
| `employment_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `ON_LEAVE` / `TERMINATED` |

- **Constraints:** CHECK status / level lists; CHECK `(employment_status = 'TERMINATED') = (termination_date IS NOT NULL)`; CHECK `termination_date >= hire_date`; CHECK `manager_employee_id <> employee_id`; CHECK `approval_limit >= 0 AND monthly_gross_salary > 0`.
- **Indexes:** `(department_id, employment_status)`, `(warehouse_id)`, `(manager_employee_id)`.
- **Relationships:** Self-hierarchy (D19). Referenced later as creator / approver / handler on almost every document, `marketing_campaigns.owner_employee_id`, `customers.sales_employee_id`, `accounting_periods.closed_by_employee_id`.
- **Populated:** Faker names; the org structure is derived from config (per DC: 1 manager, 2–3 leads, 10–20 staff; per store 1 manager + 4–6 staff; central departments sized by volume preset). Inserted top-down by level. Salaries and approval limits come from bands per `job_level`. Over time: monthly attrition (~1–2%) with replacement hires, so documents must use employees active on the document date (V-MD-06).

### 4.3 Locations

#### `warehouses`
Stock-holding locations: distribution centres and retail stores (A1 §8.2).

| Column | Type | NN | Notes |
|---|---|---|---|
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `warehouse_code` | `VARCHAR(12)` | ✓ | UNIQUE (`DC-BLR`, `ST-BLR-01`) |
| `warehouse_name` | `VARCHAR(100)` | ✓ | UNIQUE |
| `warehouse_type` | `VARCHAR(30)` | ✓ | `DISTRIBUTION_CENTRE` / `RETAIL_STORE` |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | Part of composite FK |
| `state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states`; part of composite FK |
| `address_line1` | `VARCHAR(200)` | ✓ | Warehouse addresses never change in v1, so they're held inline |
| `address_line2` | `VARCHAR(200)` | | |
| `city` | `VARCHAR(80)` | ✓ | |
| `pincode` | `CHAR(6)` | ✓ | |
| `latitude` | `DECIMAL(9,6)` | ✓ | City-level coordinates |
| `longitude` | `DECIMAL(9,6)` | ✓ | |
| `storage_area_sqft` | `INT UNSIGNED` | ✓ | |
| `replenishment_warehouse_id` | `SMALLINT UNSIGNED` | | FK → `warehouses` (self). The DC that restocks this store |
| `opened_date` | `DATE` | ✓ | |
| `closed_date` | `DATE` | | |
| `warehouse_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `INACTIVE` |

- **Keys:** Composite FK `(company_gst_registration_id, state_id)` → `company_gst_registrations (company_gst_registration_id, state_id)`. The database guarantees a warehouse can only be registered under the GSTIN of its own state.
- **Constraints:** CHECK type / status lists; CHECK `(warehouse_type = 'RETAIL_STORE') = (replenishment_warehouse_id IS NOT NULL)`; CHECK `replenishment_warehouse_id <> warehouse_id`; CHECK `closed_date IS NULL OR closed_date >= opened_date`. The replenishing warehouse must be a DC (V-MD-08).
- **Relationships:** N : 1 GST registration; 1 : N `warehouse_locations`, `employees`; 0..1 `bank_accounts` (petty cash); 1 : 1 `cost_centers`. Later: inventory, transfers, shipments, GRNs.
- **Populated:** Setup (DC cities, store count). Stores are placed in DC cities by default (no new GSTIN). DCs are inserted before stores. Static.

#### `warehouse_locations` ✱
Bins and zones inside a warehouse for put-away and picking.

| Column | Type | NN | Notes |
|---|---|---|---|
| `warehouse_location_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `location_code` | `VARCHAR(30)` | ✓ | `A03-R12-L2-B04` (aisle-rack-level-bin) or zone code (`RCV-01`) |
| `zone` | `VARCHAR(30)` | ✓ | `RECEIVING` / `STORAGE` / `PICKING` / `PACKING` / `DISPATCH` / `QUARANTINE` / `RETURNS` / `DEFECTIVE` / `SALES_FLOOR` |
| `aisle` | `VARCHAR(5)` | | NULL for floor zones |
| `rack` | `VARCHAR(5)` | | |
| `shelf_level` | `VARCHAR(5)` | | |
| `bin` | `VARCHAR(5)` | | |
| `location_type` | `VARCHAR(30)` | ✓ | `BIN` / `PALLET` / `FLOOR` / `SHELF` |
| `capacity_units` | `INT UNSIGNED` | | Indicative capacity |
| `is_pickable` | `BOOLEAN` | ✓ | |
| `is_active` | `BOOLEAN` | ✓ | |

- **Keys:** UNIQUE `(warehouse_id, location_code)`.
- **Constraints:** CHECK zone / type lists. Every warehouse has at least one `RECEIVING`, `QUARANTINE` and `DEFECTIVE` location (V-MD-09). Stores use `SALES_FLOOR` instead of `PICKING`.
- **Relationships:** N : 1 `warehouses`. Whether inventory and inventory transactions carry a location is decided in Part 1C.
- **Populated:** Derived. A layout template per `warehouse_type`, scaled by `storage_area_sqft`. Static.

### 4.4 Customers

#### `customers` ✱
B2B and B2C customers.

| Column | Type | NN | Notes |
|---|---|---|---|
| `customer_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `customer_code` | `VARCHAR(12)` | ✓ | UNIQUE (`CUST-000123`) |
| `customer_type` | `VARCHAR(10)` | ✓ | `B2B` / `B2C` |
| `customer_segment` | `VARCHAR(30)` | ✓ | `RETAILER` / `WHOLESALER` / `INSTITUTION` / `WORKSHOP` / `CORPORATE` / `CONSUMER` |
| `customer_name` | `VARCHAR(200)` | ✓ | Business legal name (B2B) or full name (B2C) |
| `contact_person_name` | `VARCHAR(120)` | | B2B contact |
| `email` | `VARCHAR(150)` | ✓ | `.example` domain |
| `phone` | `VARCHAR(10)` | ✓ | |
| `gst_registration_type` | `VARCHAR(30)` | ✓ | `REGISTERED` / `COMPOSITION` / `UNREGISTERED` / `CONSUMER` |
| `gstin` | `CHAR(15)` | | UNIQUE (multiple NULLs allowed) |
| `pan` | `CHAR(10)` | | |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_terms` |
| `credit_limit` | `DECIMAL(18,2)` | | NULL for prepaid / COD customers |
| `sales_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`. Account manager (B2B) |
| `acquisition_marketing_channel_id` | `SMALLINT UNSIGNED` | ✓ | FK → `marketing_channels` (first touch, A1) |
| `acquisition_marketing_campaign_id` | `BIGINT UNSIGNED` | | FK → `marketing_campaigns` |
| `acquisition_date` | `DATE` | ✓ | "Customer since". First order is on or after this date |
| `customer_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `ON_HOLD` / `BLOCKED` / `INACTIVE` |

- **Constraints:**
  - CHECK lists for type, segment, registration type, status.
  - CHECK `(customer_type = 'B2C') = (customer_segment = 'CONSUMER')`.
  - CHECK `(customer_type = 'B2C') = (gst_registration_type = 'CONSUMER')`.
  - CHECK `(gst_registration_type IN ('REGISTERED','COMPOSITION')) = (gstin IS NOT NULL)`.
  - GSTIN format; CHECK `gstin IS NULL OR pan IS NULL OR SUBSTRING(gstin,3,10) = pan`.
  - CHECK `credit_limit IS NULL OR credit_limit > 0`.
- **Indexes:** `(customer_type, customer_status)`, `(sales_employee_id)`, `(acquisition_date)`, `(acquisition_marketing_campaign_id)`.
- **Relationships:** 1 : N `customer_addresses`; N : 1 payment terms, employee, marketing channel, campaign. Later: orders, invoices, payments, AR.
- **Populated:** Faker (`en_IN`) for names and contacts; the type mix and segment mix come from the industry pack. B2B: mostly `REGISTERED` (~85%), with some `COMPOSITION` / `UNREGISTERED` small retailers; Net 15–60 terms and credit limits scaled by segment. B2C: `ADVANCE` or `COD` terms, no credit limit. The acquisition channel is drawn from the pack's marketing mix for the customer's type; the campaign is set only if `acquisition_date` falls inside a campaign window on that channel (V-MD-12). Over time: new customers arrive daily (acquisition curve follows seasonality); some B2B customers go `ON_HOLD` when overdue (driven by AR ageing in Phase 3); dormant B2C customers become `INACTIVE`.

#### `customer_addresses` ✱
Billing and shipping addresses, versioned (1A §3 address history).

| Column | Type | NN | Notes |
|---|---|---|---|
| `customer_address_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK → `customers` |
| `address_label` | `VARCHAR(60)` | ✓ | "Registered Office", "Godown 2", "Home" |
| `is_billing_address` | `BOOLEAN` | ✓ | |
| `is_shipping_address` | `BOOLEAN` | ✓ | One row can be both, so no duplicated text |
| `is_default_billing` | `BOOLEAN` | ✓ | |
| `is_default_shipping` | `BOOLEAN` | ✓ | |
| `contact_name` | `VARCHAR(120)` | | |
| `contact_phone` | `VARCHAR(10)` | | |
| `address_line1` | `VARCHAR(200)` | ✓ | |
| `address_line2` | `VARCHAR(200)` | | |
| `landmark` | `VARCHAR(100)` | | Common in Indian addresses |
| `city` | `VARCHAR(80)` | ✓ | |
| `state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states`. Decides place of supply |
| `pincode` | `CHAR(6)` | ✓ | |
| `previous_customer_address_id` | `BIGINT UNSIGNED` | | FK → `customer_addresses` (self). The version this row replaced |
| `valid_from` | `DATE` | ✓ | Business date the address took effect |
| `default_billing_key` | generated | | `IF(is_default_billing AND deleted_at IS NULL, customer_id, NULL)` STORED |
| `default_shipping_key` | generated | | Same pattern for shipping |

- **Keys:** UNIQUE `(default_billing_key)`, UNIQUE `(default_shipping_key)` (§2.4b); UNIQUE `(previous_customer_address_id)` (a version is replaced at most once).
- **Constraints:** PIN format; CHECK `is_billing_address OR is_shipping_address`; CHECK `NOT is_default_billing OR is_billing_address`; CHECK `NOT is_default_shipping OR is_shipping_address`.
- **Indexes:** `(customer_id, deleted_at)`, `(state_id)`, `(pincode)`.
- **Relationships:** N : 1 `customers`, `states`; self-chain of versions. Later: orders and invoices reference the address row valid at the time.
- **Rule:** A used address is never updated in place. A change inserts a new row (`previous_customer_address_id` = old row, `valid_from` = change date) and sets `deleted_at` on the old one in the same transaction.
- **Populated:** Faker. City, state and PIN prefix are drawn together from a seed list of Indian cities (`config/seed/cities.yaml`), so they're always consistent. Customer locations are weighted towards warehouse regions. B2C: one address with both flags. B2B: one billing + shipping row, ~30% with 1–3 extra shipping addresses. For REGISTERED / COMPOSITION customers the billing state must match the GSTIN state code (V-MD-11). Over time: ~5% of customers per year change an address (new version).

### 4.5 Suppliers

#### `suppliers` ✱
Goods and service vendors (R9).

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_code` | `VARCHAR(12)` | ✓ | UNIQUE (`SUP-00042`) |
| `supplier_name` | `VARCHAR(200)` | ✓ | Fictional legal name |
| `supplier_type` | `VARCHAR(10)` | ✓ | `GOODS` / `SERVICES` / `BOTH` |
| `gst_registration_type` | `VARCHAR(30)` | ✓ | `REGISTERED` / `COMPOSITION` / `UNREGISTERED` |
| `gstin` | `CHAR(15)` | | UNIQUE |
| `pan` | `CHAR(10)` | | |
| `msme_category` | `VARCHAR(10)` | | `MICRO` / `SMALL` / `MEDIUM`; NULL = not MSME |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_terms` |
| `default_expense_category_id` | `SMALLINT UNSIGNED` | | FK → `expense_categories`. For service vendors (landlord → Rent) |
| `contact_person_name` | `VARCHAR(120)` | ✓ | |
| `email` | `VARCHAR(150)` | ✓ | `.example` domain |
| `phone` | `VARCHAR(10)` | ✓ | |
| `supplier_rating` | `DECIMAL(3,2)` | ✓ | 1.00–5.00, recalculated quarterly from delivery performance |
| `onboarded_date` | `DATE` | ✓ | |
| `supplier_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `ON_HOLD` / `BLOCKED` / `INACTIVE` |

- **Constraints:** CHECK lists; CHECK `(gst_registration_type IN ('REGISTERED','COMPOSITION')) = (gstin IS NOT NULL)`; GSTIN / PAN formats and GSTIN ↔ PAN; CHECK `supplier_rating BETWEEN 1 AND 5`; CHECK `supplier_type = 'GOODS' OR default_expense_category_id IS NOT NULL`. MSME vendors must have terms of 45 days or less (V-MD-14, cross-table).
- **Indexes:** `(supplier_type, supplier_status)`.
- **Relationships:** 1 : N `supplier_addresses`, `supplier_products`; 0..1 `shipping_carriers`. Later: RFQs, POs, supplier invoices, expenses, AP.
- **Populated:** Faker names with industry-flavoured suffixes ("… Electricals", "… Polymers"). Goods suppliers come from pack templates (manufacturers, wholesalers). Service suppliers are seeded per expense category (landlord per warehouse, utility boards, ISP, cloud/SaaS vendors, carriers, auditors). Composition / unregistered vendors give no input tax credit in Part 1E. Over time: ratings move with on-time performance; occasional `ON_HOLD`; a few new suppliers onboard each quarter.

#### `supplier_addresses` ✱
Same versioning rule as customer addresses.

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_address_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `address_type` | `VARCHAR(30)` | ✓ | `REGISTERED` / `DISPATCH` |
| `is_default` | `BOOLEAN` | ✓ | Default for its type |
| `address_line1` | `VARCHAR(200)` | ✓ | |
| `address_line2` | `VARCHAR(200)` | | |
| `city` | `VARCHAR(80)` | ✓ | |
| `state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states` |
| `pincode` | `CHAR(6)` | ✓ | |
| `contact_phone` | `VARCHAR(10)` | | |
| `previous_supplier_address_id` | `BIGINT UNSIGNED` | | FK → self, UNIQUE |
| `valid_from` | `DATE` | ✓ | |
| `default_key` | generated | | `IF(is_default AND deleted_at IS NULL, CONCAT(supplier_id, ':', address_type), NULL)` STORED, `VARCHAR(40)` |

- **Keys:** UNIQUE `(default_key)`.
- **Constraints:** CHECK type list; PIN format. The registered address state equals the GSTIN state (V-MD-11).
- **Populated:** Faker + city seed list. Goods suppliers cluster in manufacturing hubs from the pack (e.g. Electronics: Noida, Chennai, Pune). Each supplier has a REGISTERED address; ~40% have a separate DISPATCH address. Rare changes over time.

#### `supplier_products` ✱
Approved supplier list: who may supply which product, at what reference cost and lead time (R17).

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_product_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `supplier_sku` | `VARCHAR(40)` | | Supplier's own item code |
| `reference_unit_cost` | `DECIMAL(18,4)` | ✓ | Typical price excluding GST. Quotations vary around it |
| `minimum_order_quantity` | `DECIMAL(18,3)` | ✓ | |
| `lead_time_days` | `SMALLINT UNSIGNED` | ✓ | Typical days from PO to delivery |
| `is_preferred` | `BOOLEAN` | ✓ | |
| `approved_date` | `DATE` | ✓ | |
| `supplier_product_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `SUSPENDED` / `INACTIVE` |
| `preferred_key` | generated | | `IF(is_preferred AND supplier_product_status = 'ACTIVE' AND deleted_at IS NULL, product_id, NULL)` STORED |

- **Keys:** UNIQUE `(supplier_id, product_id)`; UNIQUE `(preferred_key)` (at most one preferred supplier per product).
- **Constraints:** CHECK `reference_unit_cost > 0`, `minimum_order_quantity > 0`, `lead_time_days BETWEEN 1 AND 120`; CHECK status list.
- **Indexes:** `(product_id, supplier_product_status)` (the RFQ supplier lookup).
- **Relationships:** M : N bridge between `suppliers` and `products`. The supplier must be GOODS or BOTH (V-MD-15).
- **Populated:** Derived. Each active product gets 2–5 goods suppliers from the same pack; one is preferred. Reference cost = list price × (1 − pack margin band) ± supplier variation; better-rated suppliers are slightly dearer and faster. Over time: reference cost drifts with inflation (~0.3–0.8% per month, from pack); approvals are added and suspended.

### 4.6 Product Catalogue

#### `product_categories`
Hierarchical categories (self-referencing), max 3 levels, each owned by one business unit.

| Column | Type | NN | Notes |
|---|---|---|---|
| `product_category_id` | `INT UNSIGNED` | ✓ | PK |
| `category_code` | `VARCHAR(20)` | ✓ | UNIQUE (`ELEC-AUD-EAR`) |
| `category_name` | `VARCHAR(100)` | ✓ | |
| `parent_category_id` | `INT UNSIGNED` | | Self-FK (composite, below) |
| `business_unit_id` | `SMALLINT UNSIGNED` | ✓ | FK → `business_units` |
| `category_level` | `TINYINT UNSIGNED` | ✓ | 1–3 |
| `is_leaf` | `BOOLEAN` | ✓ | Products may only be assigned to leaves |
| `return_window_days` | `SMALLINT UNSIGNED` | ✓ | Days after delivery a return may be requested; 0 = not returnable |
| `is_active` | `BOOLEAN` | ✓ | |

- **Keys:** UNIQUE `(product_category_id, business_unit_id)`; composite self-FK `(parent_category_id, business_unit_id)` → `(product_category_id, business_unit_id)`, so a child is always in its parent's business unit; UNIQUE `(parent_category_id, category_name)`.
- **Constraints:** CHECK `category_level BETWEEN 1 AND 3`; CHECK `(category_level = 1) = (parent_category_id IS NULL)`. Child level = parent level + 1, and `is_leaf` matches whether the category has children (V-MD-16).
- **Populated:** Pack (the category tree is defined in YAML). Inserted level by level.

#### `product_brands`
Fictional brands.

| Column | Type | NN | Notes |
|---|---|---|---|
| `product_brand_id` | `INT UNSIGNED` | ✓ | PK |
| `brand_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `brand_name` | `VARCHAR(100)` | ✓ | UNIQUE. Fictional ("Voltrix", "Saanvi Naturals") |
| `brand_tier` | `VARCHAR(30)` | ✓ | `ECONOMY` / `MID` / `PREMIUM`. Drives the price band |
| `is_private_label` | `BOOLEAN` | ✓ | Company's own label (higher margin) |
| `is_active` | `BOOLEAN` | ✓ | |

- **Populated:** Pack (brand list per industry). Static.

#### `product_units_of_measure`
Units of measure (D18: one base UoM per product).

| Column | Type | NN | Notes |
|---|---|---|---|
| `unit_of_measure_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `uom_code` | `VARCHAR(10)` | ✓ | UNIQUE (`PCS`, `BOX`, `KG`, `LTR`, `MTR`, `PKT`, `SET`, `PAIR`, `ROLL`, `REAM`, `DOZ`, `BTL`) |
| `uom_name` | `VARCHAR(50)` | ✓ | |
| `uom_category` | `VARCHAR(30)` | ✓ | `COUNT` / `WEIGHT` / `VOLUME` / `LENGTH` |
| `decimal_places` | `TINYINT UNSIGNED` | ✓ | 0 for count units, 3 for KG/LTR. Quantities are rounded to this |
| `gst_uqc` | `VARCHAR(3)` | ✓ | GST Unique Quantity Code reported in returns (`NOS`, `KGS`, `LTR`, `BOX`, `PAC`, `PRS`, `MTR`, `ROL`, `SET`, `DOZ`, `BTL`) |

- **Constraints:** CHECK category list; CHECK `decimal_places <= 3` (matches `DECIMAL(18,3)` quantities).
- **Populated:** Seed. Static.

#### `products` ✱
Sellable and purchasable SKUs.

| Column | Type | NN | Notes |
|---|---|---|---|
| `product_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `sku` | `VARCHAR(30)` | ✓ | UNIQUE (`ELEC-EAR-VLX-0042-BLK`) |
| `product_name` | `VARCHAR(200)` | ✓ | From pack name patterns |
| `product_description` | `VARCHAR(500)` | | |
| `business_unit_id` | `SMALLINT UNSIGNED` | ✓ | Part of composite FK |
| `product_category_id` | `INT UNSIGNED` | ✓ | Part of composite FK; must be a leaf |
| `product_brand_id` | `INT UNSIGNED` | ✓ | FK → `product_brands` |
| `base_unit_of_measure_id` | `SMALLINT UNSIGNED` | ✓ | FK → `product_units_of_measure` |
| `hsn_code` | `VARCHAR(8)` | ✓ | Determines the GST rate (via the pack) |
| `style_code` | `VARCHAR(30)` | | Groups size/colour variants of one style (fashion) |
| `variant_size` | `VARCHAR(20)` | | |
| `variant_colour` | `VARCHAR(30)` | | |
| `list_price` | `DECIMAL(18,4)` | ✓ | Current selling price per base unit, **excluding GST** (Q6) |
| `mrp` | `DECIMAL(18,4)` | | Maximum Retail Price, **including GST**; mandatory for packaged consumer goods |
| `pack_size` | `DECIMAL(18,3)` | | Informational ("box of 12") |
| `weight_kg` | `DECIMAL(10,3)` | ✓ | Shipping weight per base unit |
| `volume_litres` | `DECIMAL(10,3)` | ✓ | For volumetric weight and capacity |
| `is_batch_tracked` | `BOOLEAN` | ✓ | |
| `has_expiry` | `BOOLEAN` | ✓ | |
| `shelf_life_days` | `SMALLINT UNSIGNED` | | Required when `has_expiry` |
| `warranty_months` | `TINYINT UNSIGNED` | | Electronics; drives warranty returns |
| `is_returnable` | `BOOLEAN` | ✓ | E.g. FALSE for perishables and innerwear |
| `reorder_level` | `DECIMAL(18,3)` | ✓ | Default per warehouse (Q8) |
| `reorder_quantity` | `DECIMAL(18,3)` | ✓ | |
| `launch_date` | `DATE` | ✓ | Not sellable or purchasable before this |
| `discontinued_date` | `DATE` | | |
| `product_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `DISCONTINUED` |

- **Keys:** Composite FK `(product_category_id, business_unit_id)` → `product_categories (product_category_id, business_unit_id)`, so a product's BU always matches its category's BU.
- **Constraints:** HSN format; CHECK `list_price > 0`; CHECK `mrp IS NULL OR mrp >= list_price`; CHECK `weight_kg > 0 AND volume_litres > 0`; CHECK `NOT has_expiry OR (shelf_life_days IS NOT NULL AND is_batch_tracked)`; CHECK `reorder_level >= 0 AND reorder_quantity > 0`; CHECK `(product_status = 'DISCONTINUED') = (discontinued_date IS NOT NULL)`; CHECK `discontinued_date IS NULL OR discontinued_date >= launch_date`.
- **Indexes:** `(business_unit_id, product_status)`, `(product_category_id)`, `(product_brand_id)`, `(hsn_code)`, `(style_code)`.
- **Relationships:** N : 1 BU, category, brand, UoM; 1 : N `product_tax_rates`, `supplier_products`. Later: every document line.
- **Cross-table rules:** List price + GST at the current rate ≤ MRP (Legal Metrology: never sell above MRP, V-MD-18); products sharing an HSN code have the same GST rate on any date (V-MD-19).
- **Populated:** Pack: names from patterns × brand × variant; price from the band for category × brand tier; HSN and attributes from the category template. ~85% of products launch before the history start; the rest launch through the simulated period (new-launch curve). Over time: periodic price revisions (in place, Q6); ~3–5% discontinued per year (pre-existing orders still complete).

#### `product_tax_rates`
Effective-dated GST rate per product (D17).

| Column | Type | NN | Notes |
|---|---|---|---|
| `product_tax_rate_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `gst_rate` | `DECIMAL(7,4)` | ✓ | Total GST %, e.g. `18.0000`. Intra-state = CGST ½ + SGST/UTGST ½; inter-state = IGST in full |
| `effective_from` | `DATE` | ✓ | |
| `effective_to` | `DATE` | | NULL = currently valid |
| `change_reason` | `VARCHAR(30)` | ✓ | `INITIAL` / `GST_RATE_REVISION` / `RECLASSIFICATION` |
| `notification_reference` | `VARCHAR(60)` | | Free text, e.g. "GST rate rationalisation, 22-Sep-2025" |
| `current_key` | generated | | `IF(effective_to IS NULL, product_id, NULL)` STORED |

- **Keys:** UNIQUE `(product_id, effective_from)`; UNIQUE `(current_key)` (exactly one open-ended row per product).
- **Constraints:** CHECK `gst_rate BETWEEN 0 AND 50`. The actual slabs (0, 0.25, 3, 5, 12, 18, 28, 40) are validated against the pack (V-MD-20) rather than hardcoded in a CHECK, so a future slab doesn't need a schema change. CHECK `effective_to IS NULL OR effective_to >= effective_from`; CHECK change-reason list. No overlapping periods per product, and no gaps from `launch_date` onwards (V-MD-21).
- **Lookup rule:** rate for product P on date D = the row with `effective_from <= D AND (effective_to IS NULL OR effective_to >= D)`.
- **Populated:** Pack. One `INITIAL` row from launch. When a pack-defined rate change falls inside the simulated window, the old row gets `effective_to = change date − 1` and a new `GST_RATE_REVISION` row is inserted, in the same transaction.

### 4.7 Commercial Reference

#### `payment_methods`

| Column | Type | NN | Notes |
|---|---|---|---|
| `payment_method_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `method_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `method_name` | `VARCHAR(60)` | ✓ | |
| `is_electronic` | `BOOLEAN` | ✓ | |
| `usable_for_receipts` | `BOOLEAN` | ✓ | Customer payments |
| `usable_for_payments` | `BOOLEAN` | ✓ | Supplier / expense payments |
| `settlement_lag_days` | `TINYINT UNSIGNED` | ✓ | Payment date → bank value date |
| `gateway_fee_percent` | `DECIMAL(7,4)` | | Charged on receipts (→ payment gateway charges) |
| `min_amount` | `DECIMAL(18,2)` | | |
| `max_amount` | `DECIMAL(18,2)` | | |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** CHECK `usable_for_receipts OR usable_for_payments`; CHECK `min_amount IS NULL OR max_amount IS NULL OR min_amount <= max_amount`.
- **Populated:** Seed:

| Code | Lag | Fee | Limits / notes |
|---|---|---|---|
| `NEFT` | 0 | — | |
| `RTGS` | 0 | — | Min ₹2,00,000 |
| `IMPS` | 0 | — | Max ₹5,00,000 |
| `UPI` | 1 | 0% | Max ₹1,00,000 per transaction (B2C receipts) |
| `CARD` | 2 | ~1.8% | Receipts only |
| `NET_BANKING` | 1 | ~1.2% | Receipts only |
| `CHEQUE` | 2 | — | Clearing delay; can bounce (payment status, D5) |
| `CASH` | 0 | — | Receipts max ₹1,99,999.99 (legal cash-receipt limit); the generator also caps cash expense payments at ₹10,000 |

#### `payment_terms`

| Column | Type | NN | Notes |
|---|---|---|---|
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `term_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `term_name` | `VARCHAR(60)` | ✓ | |
| `due_days` | `SMALLINT UNSIGNED` | ✓ | Days from invoice date to due date |
| `is_prepaid` | `BOOLEAN` | ✓ | Payment required before dispatch |
| `early_payment_discount_percent` | `DECIMAL(7,4)` | | E.g. 2% if paid within 10 days |
| `early_payment_discount_days` | `SMALLINT UNSIGNED` | | |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** CHECK both discount columns NULL or both set; CHECK `early_payment_discount_days < due_days`; CHECK `NOT is_prepaid OR due_days = 0`.
- **Populated:** Seed: `ADVANCE` (0, prepaid), `COD` (0), `IMMEDIATE` (0), `NET15`, `NET30`, `NET45`, `NET60`, `2_10_NET30` (2% / 10 days). Used by both customers and suppliers.

#### `shipping_carriers`
Fictional logistics partners.

| Column | Type | NN | Notes |
|---|---|---|---|
| `shipping_carrier_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `carrier_code` | `VARCHAR(12)` | ✓ | UNIQUE |
| `carrier_name` | `VARCHAR(100)` | ✓ | UNIQUE, fictional ("Swiftline Express") |
| `carrier_type` | `VARCHAR(30)` | ✓ | `EXPRESS_COURIER` / `SURFACE_LTL` / `FULL_TRUCKLOAD` / `HYPERLOCAL` / `IN_HOUSE_FLEET` |
| `supplier_id` | `BIGINT UNSIGNED` | | FK → `suppliers`, UNIQUE. The vendor record that bills freight |
| `supports_cod` | `BOOLEAN` | ✓ | |
| `supports_reverse_pickup` | `BOOLEAN` | ✓ | Customer returns |
| `tracking_number_prefix` | `VARCHAR(6)` | ✓ | UNIQUE. Makes carrier tracking numbers recognisable |
| `tracking_url_template` | `VARCHAR(200)` | | `https://track.swiftline.example/{awb}` |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** CHECK type list; CHECK `carrier_type = 'IN_HOUSE_FLEET' OR supplier_id IS NOT NULL`.
- **Populated:** Seed templates (2 express, 2 surface LTL, 1 FTL, 1 hyperlocal); a SERVICES supplier is created for each. Performance (transit days, failure rates) lives in config, not in this table. Static.

#### `sales_channels`
Where the order was placed (A1 §8.1).

| Column | Type | NN | Notes |
|---|---|---|---|
| `sales_channel_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `channel_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `channel_name` | `VARCHAR(60)` | ✓ | |
| `is_online` | `BOOLEAN` | ✓ | |
| `requires_shipment` | `BOOLEAN` | ✓ | Default fulfilment; the order's `fulfilment_type` is final |
| `allowed_customer_type` | `VARCHAR(10)` | ✓ | `B2B` / `B2C` / `BOTH` |
| `commission_percent` | `DECIMAL(7,4)` | | Marketplace commission (→ marketplace commission expense) |
| `is_active` | `BOOLEAN` | ✓ | |

- **Populated:** Seed: `WEBSITE`, `MOBILE_APP`, `WHATSAPP`, `PHONE`, `MARKETPLACE` (commission ~12–18%, set per pack), `RETAIL_STORE` (no shipment), `FIELD_SALES` (B2B).

#### `marketing_channels`
How the customer came to us.

| Column | Type | NN | Notes |
|---|---|---|---|
| `marketing_channel_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `channel_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `channel_name` | `VARCHAR(60)` | ✓ | |
| `channel_group` | `VARCHAR(30)` | ✓ | `PAID_SOCIAL` / `PAID_SEARCH` / `ORGANIC` / `OWNED` / `REFERRAL` / `OFFLINE` / `DIRECT` |
| `is_paid` | `BOOLEAN` | ✓ | |
| `supports_campaigns` | `BOOLEAN` | ✓ | Campaigns may only be created on these |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** CHECK group list; CHECK `channel_group NOT IN ('PAID_SOCIAL','PAID_SEARCH') OR is_paid`; CHECK `channel_group NOT IN ('ORGANIC','DIRECT') OR NOT is_paid`.
- **Populated:** Seed: `INSTAGRAM`, `FACEBOOK`, `YOUTUBE` (PAID_SOCIAL); `GOOGLE_ADS` (PAID_SEARCH); `ORGANIC_SEARCH` (ORGANIC); `EMAIL` (OWNED, campaigns allowed, not paid); `REFERRAL`; `TRADE_SHOW` (OFFLINE, paid); `FIELD_SALES` (OFFLINE); `DIRECT` (DIRECT / returning). Plus `MARKETPLACE_ADS` (PAID_SEARCH). Note: `OWNED` is a seventh group alongside A1's six, so Email can carry campaigns without being counted as paid media.

#### `marketing_campaigns`
Paid or owned-media campaigns with a window and budget.

| Column | Type | NN | Notes |
|---|---|---|---|
| `marketing_campaign_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `campaign_code` | `VARCHAR(20)` | ✓ | UNIQUE (`CMP-2026-014`) |
| `campaign_name` | `VARCHAR(150)` | ✓ | "Diwali Mega Sale – Instagram" |
| `marketing_channel_id` | `SMALLINT UNSIGNED` | ✓ | FK → `marketing_channels` |
| `business_unit_id` | `SMALLINT UNSIGNED` | | FK → `business_units`; NULL = company-wide |
| `target_customer_type` | `VARCHAR(10)` | ✓ | `B2B` / `B2C` / `ALL` |
| `season_tag` | `VARCHAR(30)` | | `DIWALI`, `MONSOON`, `WEDDING`, `BACK_TO_SCHOOL`, `FY_END`, `REPUBLIC_DAY`… |
| `start_date` | `DATE` | ✓ | |
| `end_date` | `DATE` | ✓ | |
| `budget_amount` | `DECIMAL(18,2)` | ✓ | Marketing expense lines tagged to the campaign roughly add up to this (A1 rule) |
| `owner_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` (Marketing department) |
| `campaign_status` | `VARCHAR(30)` | ✓ | `PLANNED` / `ACTIVE` / `COMPLETED` / `CANCELLED` |

- **Constraints:** CHECK `end_date >= start_date`; CHECK `budget_amount >= 0`; CHECK lists. The channel must have `supports_campaigns` (V-MD-22).
- **Indexes:** `(marketing_channel_id, start_date, end_date)` (campaign lookup when attributing orders and customers).
- **Populated:** Pack seasonality calendar × channel mix → ~8 per year, created ~30 days before the start as `PLANNED`; status advances with the simulated clock; ~5% `CANCELLED`.

### 4.8 Finance Master

#### `expense_categories`
Flat list of expense categories, grouped and mapped to GL accounts (R7).

| Column | Type | NN | Notes |
|---|---|---|---|
| `expense_category_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `category_code` | `VARCHAR(20)` | ✓ | UNIQUE (`RENT`, `ELECTRICITY`, `SAAS`, `AI_TOOLS`, `CLOUD`) |
| `category_name` | `VARCHAR(100)` | ✓ | UNIQUE |
| `category_group` | `VARCHAR(30)` | ✓ | `OCCUPANCY` / `UTILITIES` / `PEOPLE` / `TECHNOLOGY` / `MARKETING` / `LOGISTICS` / `PROFESSIONAL` / `ADMIN` / `FINANCE` |
| `gl_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts` (must be an EXPENSE or COGS account) |
| `default_gst_rate` | `DECIMAL(7,4)` | | NULL = outside GST (salaries, bank interest) |
| `is_itc_eligible` | `BOOLEAN` | ✓ | FALSE for blocked credits (staff food, personal travel). GST then goes to the expense, not input credit |
| `recurrence` | `VARCHAR(20)` | ✓ | `MONTHLY` / `QUARTERLY` / `ANNUAL` / `AD_HOC` |
| `payee_type` | `VARCHAR(20)` | ✓ | `SUPPLIER` / `EMPLOYEE` (reimbursement, e.g. travel) |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** CHECK lists; CHECK `NOT is_itc_eligible OR default_gst_rate > 0`.
- **Populated:** Seed. ~30 categories: rent, electricity, water, internet & telephone, SaaS subscriptions, AI tool subscriptions, cloud hosting, salaries & wages, staff welfare, repairs & maintenance, security services, housekeeping, travel & conveyance, digital ads, print & events, freight outward, courier, marketplace commission, packaging materials, audit fees, legal & consulting, printing & stationery, office supplies, petty cash expenses, bank charges, payment gateway charges, insurance. Static.

#### `bank_accounts`
Company bank accounts and petty-cash floats; the FK target for every cash movement (R16).

| Column | Type | NN | Notes |
|---|---|---|---|
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `account_code` | `VARCHAR(20)` | ✓ | UNIQUE (`BNK-COLL-01`, `PETTY-DC-BLR`) |
| `account_name` | `VARCHAR(100)` | ✓ | |
| `account_type` | `VARCHAR(30)` | ✓ | `CURRENT` / `CASH_CREDIT` / `PETTY_CASH` |
| `account_purpose` | `VARCHAR(30)` | ✓ | `COLLECTIONS` / `PAYMENTS` / `PAYROLL` / `GENERAL` / `PETTY_CASH` |
| `bank_name` | `VARCHAR(100)` | | Fictional ("Deccan Commercial Bank") |
| `branch_name` | `VARCHAR(100)` | | |
| `ifsc_code` | `CHAR(11)` | | |
| `account_number` | `VARCHAR(20)` | | UNIQUE, synthetic |
| `currency_code` | `CHAR(3)` | ✓ | = `companies.base_currency_code` (V-MD-04) |
| `overdraft_limit` | `DECIMAL(18,2)` | | Cash-credit facility limit |
| `warehouse_id` | `SMALLINT UNSIGNED` | | FK → `warehouses`. Location of a petty-cash float |
| `gl_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts`, UNIQUE (one GL account per bank account) |
| `opened_date` | `DATE` | ✓ | |
| `account_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `CLOSED` |

- **Constraints:** IFSC format; CHECK lists; CHECK `(account_type = 'PETTY_CASH') = (warehouse_id IS NOT NULL)`; CHECK `account_type = 'PETTY_CASH' OR (bank_name IS NOT NULL AND ifsc_code IS NOT NULL AND account_number IS NOT NULL)`; CHECK `(account_type = 'CASH_CREDIT') = (overdraft_limit IS NOT NULL)`.
- **No opening balance column** (D22).
- **Populated:** Setup / derived: one collections current account, one payments/payroll current account, one cash-credit account at HQ, plus one petty-cash float per warehouse. Static.

### 4.9 Accounting Master

#### `account_types`

| Column | Type | NN | Notes |
|---|---|---|---|
| `account_type_id` | `TINYINT UNSIGNED` | ✓ | PK |
| `account_type_code` | `VARCHAR(20)` | ✓ | UNIQUE |
| `account_type_name` | `VARCHAR(60)` | ✓ | |
| `normal_balance` | `VARCHAR(6)` | ✓ | `DEBIT` / `CREDIT` |
| `financial_statement` | `VARCHAR(20)` | ✓ | `BALANCE_SHEET` / `PROFIT_AND_LOSS` |
| `code_prefix` | `CHAR(1)` | ✓ | UNIQUE. First digit of account codes of this type |
| `display_order` | `TINYINT UNSIGNED` | ✓ | |

- **Populated:** Seed:

| Code | Normal balance | Statement | Prefix |
|---|---|---|---|
| `ASSET` | DEBIT | BALANCE_SHEET | 1 |
| `LIABILITY` | CREDIT | BALANCE_SHEET | 2 |
| `EQUITY` | CREDIT | BALANCE_SHEET | 3 |
| `REVENUE` | CREDIT | PROFIT_AND_LOSS | 4 |
| `COGS` | DEBIT | PROFIT_AND_LOSS | 5 |
| `EXPENSE` | DEBIT | PROFIT_AND_LOSS | 6 |

#### `chart_of_accounts`
GL accounts (hierarchical). Headers group accounts; only postable (leaf) accounts receive journal lines.

| Column | Type | NN | Notes |
|---|---|---|---|
| `account_id` | `INT UNSIGNED` | ✓ | PK. Referenced by role-named FKs (`gl_account_id`, `revenue_account_id`…) |
| `account_code` | `VARCHAR(10)` | ✓ | UNIQUE, 5 digits |
| `account_name` | `VARCHAR(150)` | ✓ | UNIQUE |
| `account_type_id` | `TINYINT UNSIGNED` | ✓ | FK → `account_types` |
| `parent_account_id` | `INT UNSIGNED` | | Composite self-FK (below) |
| `account_level` | `TINYINT UNSIGNED` | ✓ | 1–4 |
| `is_postable` | `BOOLEAN` | ✓ | FALSE for header accounts |
| `normal_balance` | `VARCHAR(6)` | ✓ | Usually the type's; overridden for contra accounts (Sales Returns, Accumulated Depreciation) |
| `system_role` | `VARCHAR(40)` | | UNIQUE. Singleton role for posting rules (D16), list below |
| `is_control_account` | `BOOLEAN` | ✓ | Balance is driven by a subledger; no manual journals |
| `control_subledger` | `VARCHAR(20)` | | `AR` / `AP` / `INVENTORY` / `BANK` / `GST` |
| `requires_cost_center` | `BOOLEAN` | ✓ | TRUE for postable P&L accounts; enforced when posting (Part 1E) |
| `is_derived` | `BOOLEAN` | ✓ | TRUE if generated from master data (per-BU, per-bank, per-GSTIN) rather than the seed skeleton |
| `description` | `VARCHAR(255)` | | |
| `is_active` | `BOOLEAN` | ✓ | |

- **Keys:** UNIQUE `(account_id, account_type_id)`; composite self-FK `(parent_account_id, account_type_id)` → `(account_id, account_type_id)`, so a child always has its parent's type.
- **Constraints:** CHECK `account_code REGEXP '^[1-6][0-9]{4}$'`; CHECK `account_level BETWEEN 1 AND 4`; CHECK `(account_level = 1) = (parent_account_id IS NULL)`; CHECK `normal_balance IN ('DEBIT','CREDIT')`; CHECK `is_control_account = (control_subledger IS NOT NULL)`; CHECK `is_postable OR (system_role IS NULL AND NOT is_control_account)`. First digit of `account_code` = the type's `code_prefix`, and parents aren't postable (V-AC-01/02).
- **System roles (initial set; Part 1E may extend):** `AR_CONTROL`, `AP_CONTROL`, `INVENTORY`, `INVENTORY_IN_TRANSIT`, `GRNI` (goods received not invoiced), `CUSTOMER_ADVANCES`, `SUPPLIER_ADVANCES`, `PAYMENT_CLEARING` (card/UPI receipts awaiting settlement), `ACCRUED_EXPENSES`, `SALARIES_PAYABLE`, `SHARE_CAPITAL`, `RETAINED_EARNINGS`, `OPENING_BALANCE_EQUITY`, `SHIPPING_INCOME`, `ROUNDING_OFF`, `INVENTORY_WRITE_OFF`, `INVENTORY_ADJUSTMENT_GAIN`, `PURCHASE_PRICE_VARIANCE`, `FREIGHT_OUTWARD`, `MARKETPLACE_COMMISSION`, `BANK_CHARGES`, `PAYMENT_GATEWAY_CHARGES`, `INELIGIBLE_GST`. At start-up the engine checks that every role it needs exists exactly once (V-AC-04).
- **Populated:** Seed skeleton + derived accounts (§5). Static after build.

#### `accounting_periods`
Monthly periods within financial years (D7).

| Column | Type | NN | Notes |
|---|---|---|---|
| `accounting_period_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `fiscal_year` | `SMALLINT UNSIGNED` | ✓ | Start year: `2026` = FY 2026-27 |
| `fiscal_year_label` | `CHAR(7)` | ✓ | `2026-27`. Also used in document numbers |
| `period_number` | `TINYINT UNSIGNED` | ✓ | 1 = first month of the FY (April by default) |
| `period_code` | `CHAR(7)` | ✓ | UNIQUE, calendar month `2026-04` |
| `period_name` | `VARCHAR(20)` | ✓ | `Apr 2026` |
| `start_date` | `DATE` | ✓ | UNIQUE |
| `end_date` | `DATE` | ✓ | UNIQUE |
| `period_status` | `VARCHAR(30)` | ✓ | `FUTURE` / `OPEN` / `CLOSED` |
| `opened_at` | `DATETIME(6)` | | |
| `closed_at` | `DATETIME(6)` | | |
| `closed_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (Finance) |

- **Keys:** UNIQUE `(fiscal_year, period_number)`.
- **Constraints:** CHECK `period_number BETWEEN 1 AND 12`; CHECK `end_date >= start_date`; CHECK status list; CHECK `(period_status = 'CLOSED') = (closed_at IS NOT NULL AND closed_by_employee_id IS NOT NULL)`; CHECK `period_status = 'FUTURE' OR opened_at IS NOT NULL`. Periods are contiguous with no gaps or overlaps (V-AC-05).
- **Rules (for Part 1E):** Journal entries may only post into an `OPEN` period, and `posting_date` must fall inside it. A business event dated in a closed period posts on the first day of the earliest open period, with the original business date kept on the document. That gives realistic "late entries" for analytics.
- **Populated:** Derived. Every month from the start of the FY containing the history start to the end of the FY containing the simulated "now". The clock opens a period on its first day and closes it on a configurable working day of the following month (default day 7); historical runs close all past months that way. Continue mode adds the next FY's rows when needed.

#### `cost_centers`
Where costs and revenues are attributed: business unit, warehouse, department or corporate.

| Column | Type | NN | Notes |
|---|---|---|---|
| `cost_center_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `cost_center_code` | `VARCHAR(20)` | ✓ | UNIQUE (`CORP`, `BU-ELEC`, `WH-DC-BLR`, `DEPT-FIN`) |
| `cost_center_name` | `VARCHAR(100)` | ✓ | UNIQUE |
| `cost_center_type` | `VARCHAR(30)` | ✓ | `CORPORATE` / `BUSINESS_UNIT` / `WAREHOUSE` / `DEPARTMENT` |
| `business_unit_id` | `SMALLINT UNSIGNED` | | FK → `business_units`, UNIQUE |
| `warehouse_id` | `SMALLINT UNSIGNED` | | FK → `warehouses`, UNIQUE |
| `department_id` | `SMALLINT UNSIGNED` | | FK → `departments`, UNIQUE |
| `parent_cost_center_id` | `SMALLINT UNSIGNED` | | FK → self. NULL only for `CORP` |
| `is_active` | `BOOLEAN` | ✓ | |

- **Constraints:** "Exactly the FK matching the type" CHECK:
  `(cost_center_type = 'CORPORATE' AND business_unit_id IS NULL AND warehouse_id IS NULL AND department_id IS NULL) OR (cost_center_type = 'BUSINESS_UNIT' AND business_unit_id IS NOT NULL AND warehouse_id IS NULL AND department_id IS NULL) OR (…WAREHOUSE…) OR (…DEPARTMENT…)`; CHECK `(cost_center_type = 'CORPORATE') = (parent_cost_center_id IS NULL)`.
- **Usage rule (detailed in 1E):** Sales revenue / COGS lines → BU cost centre; warehouse running costs (rent, power, warehouse staff) → warehouse cost centre; central overheads → department cost centre.
- **Populated:** Derived: `CORP` root + one per BU, warehouse and department, all children of `CORP`. Static.

#### `tax_accounts`
Maps each GST component and direction, per GSTIN, to its GL account.

| Column | Type | NN | Notes |
|---|---|---|---|
| `tax_account_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK → `company_gst_registrations` |
| `tax_component` | `VARCHAR(10)` | ✓ | `CGST` / `SGST` / `UTGST` / `IGST` |
| `tax_direction` | `VARCHAR(10)` | ✓ | `INPUT` (credit, asset) / `OUTPUT` (liability) |
| `gl_account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts`, UNIQUE |

- **Keys:** UNIQUE `(company_gst_registration_id, tax_component, tax_direction)`.
- **Constraints:** CHECK lists. SGST vs UTGST must match the registration state's `intra_state_tax_component`; INPUT accounts are ASSET, OUTPUT accounts LIABILITY (V-AC-06).
- **Populated:** Derived: 6 rows per GSTIN (CGST, SGST-or-UTGST, IGST × INPUT, OUTPUT), each pointing to its derived COA account. Static.

---

## 5. Default Chart of Accounts Skeleton

Account codes live in `config/seed/chart_of_accounts.yaml`, not in Python (D16). `[role]` = `system_role`; *derived* rows are generated per BU / bank account / GSTIN.

| Code | Account | Notes |
|---|---|---|
| **10000** | **Current Assets** (header) | |
| 10100 | Cash and Bank (header) | |
| 101nn | *Bank – <account>* | derived per CURRENT bank account, control BANK |
| 102nn | *Petty Cash – <warehouse>* | derived per petty-cash float |
| 10300 | Payment Clearing – Cards/UPI | [PAYMENT_CLEARING] |
| 11000 | Trade Receivables | [AR_CONTROL], control AR |
| 12000 | Inventory | [INVENTORY], control INVENTORY |
| 12100 | Inventory in Transit | [INVENTORY_IN_TRANSIT] |
| 13000 | GST Input Credit (header) | |
| 13ggx | *Input CGST/SGST/IGST – <state>* | derived, 3 per GSTIN, control GST |
| 14000 | Advances to Suppliers | [SUPPLIER_ADVANCES] |
| 14100 | Prepaid Expenses | |
| **15000** | **Non-current Assets** (header) | Only touched by the opening entry in v1 |
| 15100 | Furniture & Fixtures | |
| 15200 | Computers & Equipment | |
| 15900 | Accumulated Depreciation | contra (CREDIT) |
| **20000** | **Current Liabilities** (header) | |
| 21000 | Trade Payables | [AP_CONTROL], control AP |
| 21100 | Goods Received Not Invoiced | [GRNI] |
| 21200 | Accrued Expenses | [ACCRUED_EXPENSES] |
| 21300 | Customer Advances | [CUSTOMER_ADVANCES] |
| 21400 | Salaries Payable | [SALARIES_PAYABLE] |
| 22000 | GST Output Liability (header) | |
| 22ggx | *Output CGST/SGST/IGST – <state>* | derived, 3 per GSTIN, control GST |
| 23000 | *Cash Credit Facility – <bank>* | derived: the GL account of the CASH_CREDIT bank account (a liability, control BANK) |
| **30000** | **Equity** (header) | |
| 30100 | Share Capital | [SHARE_CAPITAL] |
| 30200 | Retained Earnings | [RETAINED_EARNINGS] |
| 30300 | Opening Balance Equity | [OPENING_BALANCE_EQUITY] |
| **40000** | **Revenue** (header) | |
| 41bb0 | *Sales – <BU>* | derived, `business_units.revenue_account_id` |
| 42bb0 | *Sales Returns – <BU>* | derived, contra (DEBIT), `sales_returns_account_id` |
| 44000 | Shipping Charges Recovered | [SHIPPING_INCOME] |
| 49000 | Other Income (header) | |
| 49100 | Interest Income | |
| 49200 | Rounding Off | [ROUNDING_OFF] (invoice totals rounded to the rupee) |
| 49300 | Inventory Adjustment Gains | [INVENTORY_ADJUSTMENT_GAIN] |
| **50000** | **Cost of Goods Sold** (header) | |
| 51bb0 | *COGS – <BU>* | derived, `cogs_account_id` |
| 52000 | Inventory Shrinkage & Write-offs | [INVENTORY_WRITE_OFF] |
| 53000 | Purchase Price Variance | [PURCHASE_PRICE_VARIANCE] |
| **60000** | **Operating Expenses** (header) | |
| 60100–61900 | Salaries & Wages, Staff Welfare, Rent, Electricity & Water, Internet & Telephone, Software Subscriptions, AI Tool Subscriptions, Cloud Hosting, Repairs & Maintenance, Security & Housekeeping, Travel & Conveyance, Advertising & Marketing, Professional Fees, Printing & Stationery, Office Expenses, Insurance, Packaging Materials, Depreciation | Mapped from `expense_categories` |
| 62000 | Freight Outward & Courier | [FREIGHT_OUTWARD] |
| 62100 | Marketplace Commission | [MARKETPLACE_COMMISSION] |
| 62200 | Bank Charges | [BANK_CHARGES] |
| 62300 | Payment Gateway Charges | [PAYMENT_GATEWAY_CHARGES] |
| 62400 | GST – Ineligible Input Credit | [INELIGIBLE_GST] |

`bb` = business unit sequence (01–09), `gg` = GSTIN sequence, `x` = component. With 5 BUs and 5 GSTINs: ≈ 60 seed + 15 BU + 30 GST + ~11 bank/petty = **~130 accounts**.

---

## 6. Relationship Summary

All FKs are `RESTRICT` / `RESTRICT` (D15). "C" = composite FK.

| Child | Column(s) | Parent | Card. |
|---|---|---|---|
| `companies` | `registered_state_id` | `states` | N:1 |
| `company_gst_registrations` | `company_id` / `state_id` | `companies` / `states` | N:1 |
| `business_units` | `revenue_account_id`, `sales_returns_account_id`, `cogs_account_id` | `chart_of_accounts` | 1:1 each |
| `employees` | `department_id` / `warehouse_id` / `cost_center_id` / `manager_employee_id` | `departments` / `warehouses` / `cost_centers` / `employees` | N:1 |
| `warehouses` | C `(company_gst_registration_id, state_id)` | `company_gst_registrations` | N:1 |
| `warehouses` | `state_id` / `replenishment_warehouse_id` | `states` / `warehouses` | N:1 |
| `warehouse_locations` | `warehouse_id` | `warehouses` | N:1 |
| `customers` | `payment_term_id` / `sales_employee_id` / `acquisition_marketing_channel_id` / `acquisition_marketing_campaign_id` | `payment_terms` / `employees` / `marketing_channels` / `marketing_campaigns` | N:1 |
| `customer_addresses` | `customer_id` / `state_id` / `previous_customer_address_id` | `customers` / `states` / self | N:1 / N:1 / 1:1 |
| `suppliers` | `payment_term_id` / `default_expense_category_id` | `payment_terms` / `expense_categories` | N:1 |
| `supplier_addresses` | `supplier_id` / `state_id` / `previous_supplier_address_id` | `suppliers` / `states` / self | N:1 / N:1 / 1:1 |
| `supplier_products` | `supplier_id` / `product_id` | `suppliers` / `products` | M:N bridge |
| `product_categories` | `business_unit_id`; C `(parent_category_id, business_unit_id)` | `business_units`; self | N:1 |
| `products` | C `(product_category_id, business_unit_id)` / `product_brand_id` / `base_unit_of_measure_id` | `product_categories` / `product_brands` / `product_units_of_measure` | N:1 |
| `product_tax_rates` | `product_id` | `products` | N:1 |
| `shipping_carriers` | `supplier_id` | `suppliers` | 1:1 |
| `marketing_campaigns` | `marketing_channel_id` / `business_unit_id` / `owner_employee_id` | `marketing_channels` / `business_units` / `employees` | N:1 |
| `expense_categories` | `gl_account_id` | `chart_of_accounts` | N:1 |
| `bank_accounts` | `warehouse_id` / `gl_account_id` | `warehouses` / `chart_of_accounts` | N:1 / 1:1 |
| `chart_of_accounts` | `account_type_id`; C `(parent_account_id, account_type_id)` | `account_types`; self | N:1 |
| `accounting_periods` | `closed_by_employee_id` | `employees` | N:1 |
| `cost_centers` | `business_unit_id` / `warehouse_id` / `department_id` / `parent_cost_center_id` | BU / warehouse / department / self | 1:1 / 1:1 / 1:1 / N:1 |
| `tax_accounts` | `company_gst_registration_id` / `gl_account_id` | `company_gst_registrations` / `chart_of_accounts` | N:1 / 1:1 |

The ERD diagrams follow in Part 1F.

---

## 7. Validation Rules Not Expressible as Constraints

These become automated tests in Phase 3 (business-rule tests) and checks on the Control Panel Validation screen. Every one must pass on clean data.

| ID | Rule |
|---|---|
| V-MD-01 | Every master code (`*_code`, `sku`) follows its configured pattern |
| V-MD-02 | All GSTINs have a valid check digit; company GSTINs all embed the company PAN; every PAN starts with the configured synthetic prefix (D36) |
| V-MD-03 | Every GSTIN's first two digits equal the `gst_state_code` of its state (company registrations) or its registered/billing address state (suppliers/customers) |
| V-MD-04 | Exactly one company row per workspace; its `company_code` equals the workspace code; every `bank_accounts.currency_code` equals `companies.base_currency_code` |
| V-MD-05 | Exactly one employee has `manager_employee_id IS NULL`; there are no cycles in the employee hierarchy |
| V-MD-06 | Every employee referenced on a document was `ACTIVE` (hired, not terminated) on the document date and has an `approval_limit` ≥ the document value they approved |
| V-MD-07 | Every warehouse state has exactly one ACTIVE GST registration and vice versa (D3) |
| V-MD-08 | A store's `replenishment_warehouse_id` points to an active DC |
| V-MD-09 | Every warehouse has RECEIVING, QUARANTINE and DEFECTIVE locations |
| V-MD-10 | Every live customer has exactly one live default billing and one live default shipping address |
| V-MD-11 | Address city / state / PIN prefix are consistent with the city seed list; registered customers' billing state and suppliers' registered state match the GSTIN state |
| V-MD-12 | A customer's acquisition campaign is on the acquisition channel and its window contains `acquisition_date` |
| V-MD-13 | Address version chains are linear: one live row per chain, `valid_from` increasing along the chain |
| V-MD-14 | MSME suppliers have payment terms of 45 days or less |
| V-MD-15 | `supplier_products` only links GOODS/BOTH suppliers to products of the supplier's packs; every active product has ≥ 2 active approved suppliers |
| V-MD-16 | Category tree: levels consistent, `is_leaf` correct, products only on leaf categories |
| V-MD-17 | Variant products sharing a `style_code` share category, brand and HSN |
| V-MD-18 | `list_price × (1 + gst_rate/100) ≤ mrp` for every product with an MRP, on every date |
| V-MD-19 | Products with the same `hsn_code` have the same GST rate on any date |
| V-MD-20 | Every `gst_rate` is one of the pack's allowed slabs |
| V-MD-21 | `product_tax_rates` periods per product are contiguous from `launch_date`, with no overlaps |
| V-MD-22 | Campaigns are only on channels with `supports_campaigns`; owners are Marketing employees |
| V-AC-01 | Account code prefix matches its account type's `code_prefix` |
| V-AC-02 | Only leaf accounts are postable; header accounts have children |
| V-AC-03 | BU revenue / returns / COGS accounts, expense-category accounts, bank GL accounts and tax accounts point to postable accounts of the correct type |
| V-AC-04 | Every `system_role` the posting engine needs exists exactly once |
| V-AC-05 | Accounting periods are contiguous, 12 per FY, aligned to `fy_start_month`; at most the current and previous months are OPEN |
| V-AC-06 | Tax accounts: 6 per GSTIN, SGST vs UTGST per state, INPUT → ASSET, OUTPUT → LIABILITY |
| V-AC-07 | Every business unit, warehouse and department has exactly one cost centre |

---

## 8. What Comes Next

Part 1C: procurement and inventory tables (16 + 8), resolving Q1 (where moving-average cost lives), Q2 (source-document linking), Q7 (GST on inter-GSTIN stock transfers) and Q8 (per-warehouse reorder policy).
