# Phase 1 – Part 1A: Business Overview, Entity Inventory and Design Decisions

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9) – Addendum A1 added
**Last updated:** 2026-11-08

---

## 0. Approved Decisions (baseline)

| # | Decision | Approved choice |
|---|---|---|
| D1 | Inventory costing | Moving weighted average cost; batches kept for traceability only |
| D2 | Customer mix | Default ~80% B2B / ~20% B2C; each industry pack can override (e.g. Beauty is B2C-heavy) — see §8 |
| D3 | GST registrations | One company GSTIN per state where the company has a warehouse or store |
| D4 | Invoice timing | One tax invoice per shipment, raised at dispatch; counter sales at a retail store are invoiced at handover (no shipment) — see §8 |
| D5 | Payment attempts | `customer_payments` carries a status; only successful payments are allocated |
| D6 | Anomaly isolation | Separate MySQL schema: `scm_oltp` (clean) and `scm_sim` (simulation) |
| D7 | Financial year | April–March, configurable |
| D8 | Environment | MySQL 8.0.16+ installed natively (Docker optional, not required); Python 3.11+. App connects via host/port/user/password from environment variables, so the install method is invisible to the code. Dedicated MySQL user `scm_app` with privileges limited to `scm\_%` schemas (not root) |
| D9 | Review mode | Owner accepts architect recommendations; Phase 1 parts proceed without per-part review. Approval still required before Phase 2 |
| D10 | Order attribution | Every order carries a sales channel (where bought) and a marketing channel (how the customer came), optionally a campaign — see §8 |
| D11 | Industry packs | Company name and industries are user inputs; industry packs (YAML) drive catalogue, pricing, GST slabs, seasonality, channel mix — see §8 |
| D12 | Control Panel UI | Streamlit control panel added as a new phase; generation runs as a background job reporting progress to MySQL — see §8 |

---

## 1. The Fictional Company

**Kaveri Nexus Distribution Pvt. Ltd.** (fictional) — a B2B-focused distributor and trader that buys finished goods from manufacturers and wholesalers and sells them to retailers, institutions, workshops and some end consumers.

| Attribute | Value |
|---|---|
| Head office | Bengaluru, Karnataka |
| Base currency | INR |
| Tax regime | GST (CGST + SGST intra-state, IGST inter-state) |
| Financial year | 1 April – 31 March |
| Warehouses (default 5) | Bengaluru (KA), Bhiwandi (MH), Chennai (TN), Gurugram (HR), Kolkata (WB) |
| GST registrations | One per warehouse state (5 GSTINs, all synthetic) |

### Business units (product divisions)

| Business unit | Example products | Typical GST slab |
|---|---|---|
| Consumer Electronics & Accessories | Chargers, cables, earphones, power banks | 18% |
| Home & Kitchen | Cookware, mixer grinders, storage containers | 12% / 18% |
| Office & Stationery | Paper, files, printer cartridges, desk supplies | 12% / 18% |
| Industrial & MRO Supplies | Hand tools, safety gear, fasteners, lubricants | 18% |
| Packaged FMCG | Packaged foods, cleaning products, personal care | 5% / 12% / 18% |

Business units drive cost centres, product ownership and segment-level P&L reporting later in dbt.

---

## 2. Core Business Processes

### 2.1 Procure-to-Pay (P2P)
1. Purchasing raises an **RFQ** for products that are low on stock (reorder level breached) or for planned demand.
2. The RFQ is sent to 2–5 **approved suppliers** for those products.
3. Suppliers return **quotations** (some decline or never respond).
4. Purchasing **evaluates** quotations (price, lead time, supplier rating) and accepts one; others are rejected.
5. A **purchase order** is raised against the accepted quotation and approved.
6. The supplier delivers, possibly in several partial deliveries and possibly late → **goods receipts**.
7. Each receipt is **inspected**; accepted quantity enters inventory, rejected quantity is recorded and never enters stock.
8. The supplier sends a **supplier invoice**, matched against PO and GRN (**three-way match**).
9. The invoice becomes an **accounts payable** open item and is paid on or after its due date → **supplier payment**.

### 2.2 Order-to-Cash (O2C)
1. A customer places an **order** (multiple lines) against an active price.
2. Stock is **reserved** in a warehouse; if insufficient, the order waits or is partly fulfilled.
3. The warehouse picks, packs and dispatches one or more **shipments** (split shipments allowed).
4. A **tax invoice** is raised per shipment at dispatch (D4) → **accounts receivable** open item.
5. The carrier moves the shipment; tracking events arrive; delivery may fail and be re-attempted or returned to sender.
6. The customer **pays** (full, partial, multiple instalments, failed attempts); successful payments are **allocated** to invoices.
7. B2C prepaid orders are paid before dispatch; B2B orders are paid on credit terms (Net 15/30/45/60).

### 2.3 Inventory Management
- Every quantity change is an **inventory transaction** (receipt, sale issue, return receipt, transfer out/in, adjustment, write-off, status change).
- Stock carries a **status**: `SELLABLE`, `QUARANTINE`, `DEFECTIVE`.
- Reservations hold sellable stock for confirmed orders without moving it.
- Inter-warehouse **stock transfers** rebalance stock (out at source, in-transit, in at destination).
- Periodic cycle counts produce **stock adjustments** (shrinkage, damage, found stock).

### 2.4 Returns and Refunds
1. Customer requests a return against a delivered order line; eligible quantity = delivered − already returned.
2. Goods arrive → stock enters `QUARANTINE`.
3. **Inspection** decides: restock as `SELLABLE`, move to `DEFECTIVE`, or hold for further inspection.
4. A **credit note** is issued against the original invoice (reduces receivable and reverses GST).
5. If the customer had already paid, a **refund** is processed against the credit note (pending approval → completed).
6. Defective goods are registered with a reason and a disposition (scrap, repair, return to supplier).

### 2.5 Expense Management
- Recurring monthly expenses (rent, electricity, internet, SaaS/AI subscriptions, cloud hosting, salaries) and one-off expenses (repairs, travel, marketing, professional fees, stationery, petty cash).
- Each expense has a vendor or employee payee, GST components (input credit where eligible), approval status, and one or more payments.

### 2.6 Record-to-Report (R2R)
- Every financially relevant business event automatically posts a **balanced journal entry** (see Part 1E).
- AR and AP subledgers reconcile to their GL control accounts.
- Bank transactions are reconciled against payments.
- Periods are opened and closed monthly; financial-year reporting follows D7.

### 2.7 GST Compliance
- Every taxable document line produces **tax transactions** (output tax on sales, input tax on purchases and eligible expenses).
- Monthly **GST returns** per GSTIN summarise output tax, input tax credit and net payable.
- GST paid to the government settles the liability. **It is not an expense** (see §4, R10).

---

## 3. Design Conventions

| Area | Convention |
|---|---|
| Engine / charset | InnoDB, `utf8mb4`, `utf8mb4_0900_ai_ci` |
| Table names | `snake_case`, plural (`orders`, `order_line_items`) |
| Primary keys | Surrogate `BIGINT UNSIGNED AUTO_INCREMENT`, named `<entity>_id` (small reference tables may use `INT`/`SMALLINT`) |
| Business keys | Human-readable document number with a UNIQUE constraint, e.g. `SO/KA/2026-27/000123` |
| Foreign keys | Named `<referenced_entity>_id`; always declared with explicit FK constraints |
| Money | `DECIMAL(18,2)`; unit prices and costs `DECIMAL(18,4)` |
| Quantities | `DECIMAL(18,3)` (supports kg/litre UoMs) |
| Rates | `DECIMAL(7,4)` for tax and discount percentages |
| Status columns | `VARCHAR(30)` + `CHECK` constraint (MySQL ≥ 8.0.16 enforces CHECK). Chosen over ENUM (painful to evolve) and lookup tables (adds ~30 tiny tables with no business value) |
| Business dates | Explicit columns: `order_date`, `invoice_date`, `due_date`, `posting_date`, … |
| Audit columns | `created_at`, `updated_at` as `DATETIME(6)` on every table; `deleted_at` on master data that can be soft-deleted |
| Header/line pattern | `<doc>_headers` or `<doc>s` + `<doc>_line_items`, line has `line_number` unique per header |
| Status history | `<doc>_status_history` with `from_status`, `to_status`, `changed_at`, `changed_by_employee_id` |

### Business dates vs audit timestamps
- **Business dates** say *when the event happened in the business* (invoice dated 3 April).
- **Audit timestamps** (`created_at`, `updated_at`) say *when the row was written or changed in the system*.
- In historical generation both come from the simulated clock. In continuous generation the simulated clock keeps moving forward, so `updated_at` always increases. That's what makes watermark-based incremental loading reliable. Detailed in Part 1G.

### Intentional snapshot columns (justified denormalisation)
Some values are deliberately copied onto transactions because they must not change if master data changes later:
- Unit price, discount and tax rate on order, invoice and PO lines.
- Customer GSTIN, place-of-supply state and billing/shipping address IDs on invoices (a legal requirement for tax invoices).
- Moving-average unit cost on each inventory issue (needed for COGS).

This isn't redundancy. It records the facts as they were when the transaction happened.

### Address history
Customer and supplier addresses are never updated in place once used. A change creates a new address row and soft-deletes the old one, so historical orders and invoices still point at the address that was valid at the time. That's effectively SCD Type 2 inside the OLTP system, and it gives the CDC exercises realistic master-data changes.

---

## 4. Redundancy and Gap Resolution

| # | Issue in requested list | Resolution | Reason |
|---|---|---|---|
| R1 | `payment_allocations` (sales), `invoice_payments` (invoicing) and `customer_payment_allocations` (AR) all mean "payment X settles invoice Y" | **Merged** into `customer_payment_allocations` | One source of truth for applied cash; three copies would drift |
| R2 | `stock_movements` duplicates `inventory_transactions` | **Merged** into `inventory_transactions` | One movement ledger |
| R3 | `defective_stock_transactions` is a second quantity ledger | **Merged** into `inventory_transactions` using stock status `DEFECTIVE` | Inventory reconciliation must work against one ledger |
| R4 | `returned_goods` duplicates the return line + the receipt movement | **Merged**: received qty and timestamp on `sales_return_line_items`, quantity movement in `inventory_transactions` | Physical receipt is an attribute of the return line, not a separate entity |
| R5 | `refund_line_items` duplicates `credit_note_line_items` | **Dropped**. A refund pays out a credit note; line detail lives on the credit note | Credit note = accounting document; refund = cash movement |
| R6 | `general_ledger` would compete with `journal_entry_lines` | **View** `v_general_ledger` over journal lines + table `gl_period_balances` (derived, rebuildable) | Journal lines remain the only accounting source of truth |
| R7 | `expense_categories` listed in both Master Data and Expenses | **Single table** in master data | Duplicate listing |
| R8 | No tables for supplier invoices and supplier payments, although P2P requires them | **Added** `supplier_invoices`, `supplier_invoice_line_items`, `supplier_payments` | Needed for three-way match and AP |
| R9 | Expense vendors (landlord, ISP, cloud provider) vs goods suppliers | **Single `suppliers` table** with `supplier_type` = `GOODS` / `SERVICES` / `BOTH` | One vendor master; AP and ageing cover all vendors |
| R10 | "GST payments" listed as an expense | **Not an expense.** GST payment is recorded on `gst_returns` + `bank_transactions` and posts Dr Output GST / Cr Input GST / Cr Bank | Booking it as an expense would overstate costs |
| R11 | Shipping status list mixes order-level states ("Order Confirmed") with shipment states | **Split**: order statuses on `orders`, fulfilment/carrier statuses on `shipments` | Each status belongs to the entity it describes |
| R12 | `shipment_tracking` vs `shipment_status_history` overlap | **Kept, with separate responsibilities**: `shipment_status_history` = lifecycle status changes; `shipment_tracking` = carrier scan events (hub, city, scan time) | Carrier feeds are a separate, high-volume, external source, which is ideal for late/out-of-order event practice |
| R13 | GSTIN per state needed (D3) but no table | **Added** `company_gst_registrations` | Determines CGST/SGST vs IGST |
| R14 | Place of supply needs GST state codes | **Added** reference table `states` | Standard GST state codes (e.g. 29 = Karnataka) |
| R15 | Due dates and ageing need credit terms | **Added** `payment_terms` | Net 15/30/45/60, advance, COD |
| R16 | Bank transactions and reconciliation need company bank accounts | **Added** `bank_accounts` | FK target for all cash movements |
| R17 | RFQ supplier selection needs to know who supplies what | **Added** `supplier_products` (approved supplier list with lead time and reference price) | Prevents random supplier–product pairings |
| R18 | Employees need an organisational home for approvals and cost centres | **Added** `departments` | Purchasing, Warehouse, Finance, Sales, etc. |
| R19 | Stock adjustments from cycle counts contain many products | **Added** `stock_adjustment_line_items` | Header/line pattern like other documents |
| R20 | Continuous generation must never reuse document numbers | **Added** `document_sequences` | Per document type, GSTIN and FY counter; makes reruns safe |
| R21 | Incremental generation must know what was already generated | **Added** `generation_runs` | Records seed, mode, simulated clock range and status per run |
| R22 | Post-receipt returns to suppliers (purchase returns / debit notes) | **Deferred**. Rejection at GRN is handled by accepted vs rejected qty; defective stock disposition `RETURN_TO_SUPPLIER` is recorded but no debit-note document in v1 | Keeps scope controlled; can be added later without changing existing tables |

---

## 5. Final Entity Inventory

Legend: **R** = requested, **A** = added, **M** = requested but merged/changed (see §4).

### A. Master Data — schema `scm_oltp` (23 tables)

| Table | | Represents |
|---|---|---|
| `states` | A | Indian states/UTs with GST state code |
| `companies` | R | Legal entity (one row by default; table kept for extensibility) |
| `company_gst_registrations` | A | Company GSTIN per state |
| `business_units` | R | Product divisions |
| `departments` | A | Organisational departments |
| `employees` | R | Staff who create, approve and handle documents |
| `warehouses` | R | Physical warehouses, each linked to a GST registration |
| `warehouse_locations` | R | Zone/aisle/rack/bin inside a warehouse (put-away and pick locations) |
| `customers` | R | B2B and B2C customers |
| `customer_addresses` | R | Billing and shipping addresses (versioned) |
| `suppliers` | R | Goods and service vendors |
| `supplier_addresses` | R | Supplier addresses (versioned) |
| `supplier_products` | A | Approved supplier–product pairs |
| `product_categories` | R | Hierarchical categories (self-referencing parent) |
| `product_brands` | R | Brands |
| `product_units_of_measure` | R | UoM (piece, box, kg, litre…) |
| `product_tax_rates` | R | GST slabs with effective dates |
| `products` | R | Sellable/purchasable SKUs with HSN code, reorder level, list price |
| `payment_methods` | R | Bank transfer, UPI, card, cheque, cash, NEFT/RTGS |
| `payment_terms` | A | Credit terms |
| `shipping_carriers` | R | Logistics partners (fictional) |
| `expense_categories` | R | Expense classification mapped to GL accounts |
| `bank_accounts` | A | Company bank accounts mapped to GL accounts |

### B. Accounting Master Data (5 tables)

| Table | | Represents |
|---|---|---|
| `account_types` | R | Asset, Liability, Equity, Revenue, COGS, Expense (with normal balance side) |
| `chart_of_accounts` | R | GL accounts (hierarchical) |
| `accounting_periods` | R | Monthly periods within financial years, open/closed |
| `cost_centers` | R | Cost centres by business unit / warehouse / department |
| `tax_accounts` | R | Maps tax type (CGST/SGST/IGST, input/output) per GSTIN to GL accounts |

### C. Procurement (16 tables)

| Table | | Represents |
|---|---|---|
| `rfq_headers` | R | Request for quotation |
| `rfq_line_items` | R | Products and quantities requested |
| `rfq_suppliers` | R | Suppliers invited (M:N RFQ ↔ supplier), response status |
| `rfq_status_history` | R | RFQ lifecycle |
| `quotation_headers` | R | Supplier quotation, incl. evaluation score and outcome |
| `quotation_line_items` | R | Quoted price, lead time, quantity per RFQ line |
| `quotation_status_history` | R | Quotation lifecycle (received → evaluated → accepted/rejected) |
| `purchase_order_headers` | R | Purchase order |
| `purchase_order_line_items` | R | Ordered products; ordered / received / rejected / invoiced qty |
| `purchase_order_status_history` | R | PO lifecycle |
| `goods_receipts` | R | Goods receipt note (GRN), one per delivery |
| `goods_receipt_line_items` | R | Received qty per PO line |
| `goods_inspections` | R | Accepted vs rejected qty and rejection reason |
| `supplier_invoices` | A | Supplier bill |
| `supplier_invoice_line_items` | A | Billed qty/price per PO line (three-way match) |
| `supplier_payments` | A | Payments made to suppliers |

### D. Inventory (8 tables)

| Table | | Represents |
|---|---|---|
| `inventory` | R | Current balance per warehouse × product × stock status |
| `inventory_transactions` | R/M | **Single movement ledger** (absorbs `stock_movements`, `defective_stock_transactions`) |
| `inventory_batches` | R | Lot received via a GRN line (traceability, no separate balance) |
| `inventory_reservations` | R | Stock held for order lines |
| `stock_adjustments` | R | Adjustment document (cycle count, damage, shrinkage) |
| `stock_adjustment_line_items` | A | Products adjusted |
| `stock_transfers` | R | Inter-warehouse transfer document |
| `stock_transfer_line_items` | R | Products transferred; shipped / received qty |

### E. Sales & Order Management (4 tables)

| Table | | Represents |
|---|---|---|
| `orders` | R | Customer order header |
| `order_line_items` | R | Ordered products with snapshot price/discount/tax |
| `order_status_history` | R | Order lifecycle |
| `customer_payments` | R | Payment attempts and receipts (status: initiated / success / failed / reversed) |

*(`payment_allocations` → merged into `customer_payment_allocations`, R1)*

### F. Shipping & Logistics (5 tables)

| Table | | Represents |
|---|---|---|
| `shipments` | R | Dispatch from one warehouse for one order (split shipments = several rows) |
| `shipment_line_items` | R | Order lines and quantities in this shipment |
| `shipment_status_history` | R | Fulfilment lifecycle (Picking → … → Delivered) |
| `shipment_tracking` | R | Carrier scan events |
| `delivery_attempts` | R | Each attempt with outcome and failure reason |

### G. Invoicing (4 tables)

| Table | | Represents |
|---|---|---|
| `invoices` | R | Tax invoice per shipment |
| `invoice_line_items` | R | Invoiced lines (→ order line, → shipment line) with GST split |
| `credit_notes` | R | Credit note against an invoice (return, price adjustment, cancellation) |
| `credit_note_line_items` | R | Credited lines with GST reversal |

*(`invoice_payments` → merged, R1)*

### H. Returns, Refunds & Defective Stock (5 tables)

| Table | | Represents |
|---|---|---|
| `sales_returns` | R | Return authorisation (RMA) against an order |
| `sales_return_line_items` | R/M | Returned qty per order line, incl. received qty (absorbs `returned_goods`) |
| `return_inspections` | R | Inspection outcome per return line |
| `refunds` | R | Cash refund against a credit note and the original payment |
| `defective_stock` | R | Register of defective lots: reason, source, disposition |

*(`returned_goods`, `refund_line_items`, `defective_stock_transactions` → merged/dropped, R3–R5)*

### I. Expenses (4 tables)

| Table | | Represents |
|---|---|---|
| `expenses` | R | Expense bill / claim (recurring or one-off) |
| `expense_line_items` | R | Category, amount, GST, cost centre per line |
| `expense_payments` | R | Payment(s) settling an expense |
| `expense_attachments_metadata` | R | Metadata of supporting documents (no binary files) |

### J. Accounting Transactions, AR/AP & Tax (12 tables + 1 view)

| Table | | Represents |
|---|---|---|
| `journal_entries` | R | Journal header, linked to its source business document |
| `journal_entry_lines` | R | Debit/credit lines (**accounting source of truth**) |
| `v_general_ledger` | M | View: ledger by account with running balance |
| `gl_period_balances` | M | Derived opening/debit/credit/closing per account per period (rebuildable) |
| `bank_transactions` | R | Bank statement lines (receipts, payments, charges) |
| `bank_reconciliations` | R | Reconciliation per bank account per period |
| `accounts_receivable` | R | Open-item AR subledger (one row per invoice / credit note) |
| `customer_payment_allocations` | R | Payment → invoice applications |
| `accounts_payable` | R | Open-item AP subledger (one row per supplier invoice or expense) |
| `supplier_payment_allocations` | R | Supplier payment → supplier invoice applications |
| `tax_transactions` | R | GST input/output per taxable document line |
| `gst_returns` | R | Monthly GST return per GSTIN (incl. payment) |
| `gst_return_line_items` | R | Summarised tax by type / rate / section |

### K. System & Generation Control (2 tables)

| Table | | Represents |
|---|---|---|
| `document_sequences` | A | Next document number per type, GSTIN and FY |
| `generation_runs` | A | Audit of every generation run (seed, mode, simulated clock window, row counts, status) |

### L. Outside the clean OLTP schema (designed in Part 1G)

| Location | Tables | Purpose |
|---|---|---|
| MySQL `scm_sim` | `change_event_log`, anomaly registry, staging copies | CDC simulation and anomaly injection (Phase 7) |
| DuckDB `etl_meta` | `etl_load_history`, `etl_table_watermarks`, `etl_error_log` | Loader metadata (Phase 4) |

### Totals

| | Count |
|---|---|
| Clean OLTP tables (`scm_oltp`) | **92** (88 original + 4 from §8) |
| Views | 1 |
| Requested tables merged or dropped | 7 (`payment_allocations`, `invoice_payments`, `stock_movements`, `returned_goods`, `defective_stock_transactions`, `refund_line_items`, `general_ledger`→view) |
| Tables added | 19 (15 original + `sales_channels`, `marketing_channels`, `marketing_campaigns`, `generation_run_steps`) |

---

## 6. End-to-End Traceability (how the chains connect)

**Procurement:**
`rfq_headers` → `rfq_suppliers` → `quotation_headers` → `purchase_order_headers` (FK accepted quotation) → `goods_receipts` → `goods_inspections` → `inventory_transactions` (FK GRN line) → `supplier_invoice_line_items` (FK PO line) → `accounts_payable` → `supplier_payment_allocations` → `supplier_payments` → `journal_entries`

**Sales:**
`orders` → `order_line_items` → `inventory_reservations` → `shipment_line_items` → `inventory_transactions` → `invoice_line_items` (FK order line + shipment line) → `accounts_receivable` → `customer_payment_allocations` → `customer_payments` → `sales_return_line_items` (FK order line) → `return_inspections` → `credit_note_line_items` (FK invoice line) → `refunds` → `journal_entries`

Every quantity-changing row in `inventory_transactions` and every row in `journal_entries` will carry a reference back to the business document that caused it.

---

## 7. Open Design Questions (resolved in later parts)

| # | Question | Part |
|---|---|---|
| Q1 | Where moving-average cost is stored (per warehouse × product vs per product) | 1C |
| Q2 | Linking journal entries and inventory transactions to source documents: polymorphic `source_type + source_id` vs nullable FK per source | 1C / 1E |
| Q3 | AP open items referencing either a supplier invoice or an expense: nullable FKs + CHECK "exactly one" | 1E |
| Q4 | How the simulated clock and `updated_at` interact with watermarks during continuous generation | 1G |
| Q5 | Which tables are append-only (insert-only watermark) vs mutable (update watermark) vs full-reload | 1G |

---

## 8. Addendum A1 (2026-11-08): Order Attribution, Retail Stores, Industry Packs, Control Panel UI

### 8.1 Order attribution (D10)

Two different questions, kept as two different attributes:

| Attribute | Question it answers | Examples |
|---|---|---|
| **Sales channel** | *Where* was the order placed? | Website, Mobile App, WhatsApp, Phone, Marketplace, Retail Store, Field Sales Rep |
| **Marketing channel** | *How* did the customer come to us? | Instagram, Google Ads, Facebook, YouTube, Organic Search, Referral, Email, Trade Show, Direct / Returning, Field Sales |
| **Campaign** (optional) | *Which* paid campaign drove it? | "Diwali Mega Sale – Instagram", "Monsoon Kitchen – Google Ads" |

Structured FK columns are used rather than free-form text tags, because analytics (orders by channel, CAC, ROAS) needs consistent values, not typo-ridden strings.

**New tables**

| Table | Why | PK | FKs | Populated by |
|---|---|---|---|---|
| `sales_channels` | Reference list of order-placement channels, with `is_online`, `requires_shipment` flags | `sales_channel_id` | — | Seed data (static) |
| `marketing_channels` | Reference list of acquisition channels, with `channel_group` (PAID_SOCIAL / PAID_SEARCH / ORGANIC / REFERRAL / OFFLINE / DIRECT) and `is_paid` | `marketing_channel_id` | — | Seed data (static) |
| `marketing_campaigns` | A paid campaign with start/end dates and budget, on one marketing channel | `marketing_campaign_id` | `marketing_channel_id`, `business_unit_id` | Generated per quarter/festival season from the industry pack's seasonality |

**Column additions**

| Table | New columns | Meaning |
|---|---|---|
| `orders` | `sales_channel_id`, `marketing_channel_id`, `marketing_campaign_id` (nullable), `fulfilment_type` (`SHIP` / `STORE_PICKUP` / `COUNTER_SALE`) | Order-level (last-touch) attribution |
| `customers` | `acquisition_marketing_channel_id`, `acquisition_marketing_campaign_id` (nullable) | First-touch: how the customer was originally acquired |
| `expense_line_items` | `marketing_campaign_id` (nullable) | Ties ad spend to a campaign |

Linking campaigns to both orders and expense lines makes **CAC, ROAS and first-touch vs last-touch attribution** possible downstream in dbt.

**Business rules**
- B2B orders mostly come through Field Sales Rep / Phone / WhatsApp; B2C mostly Website / App / Marketplace / Store (mix from industry pack).
- A campaign may be set on an order only if the order date falls inside the campaign window and the channel matches.
- Marketing expenses for a campaign are generated within its window and roughly match its budget.

### 8.2 Retail stores (needed for the "Store" sales channel)

- `warehouses` gets `warehouse_type` = `DISTRIBUTION_CENTRE` / `RETAIL_STORE`. A store is a stock-holding location like a warehouse, so inventory, transfers and reservations work unchanged. Default config: 3 stores in cities that already have a DC (no extra GSTIN needed).
- Store replenishment happens through `stock_transfers` from the nearest DC.
- `COUNTER_SALE` orders: goods handed over at the counter, invoice raised immediately, no shipment. `invoice_line_items.shipment_line_item_id` therefore becomes nullable, with a validation rule that it is mandatory when `fulfilment_type = 'SHIP'`.
- `STORE_PICKUP`: ordered online, collected at a store; invoiced at handover.

### 8.3 Industry packs (D11)

The schema stays industry-neutral. What changes per industry is **data and behaviour**, held in YAML "industry packs" (`config/industries/<industry>.yaml`).

Initial packs:

| Industry pack | Character |
|---|---|
| Consumer Electronics | High value, 18% GST, warranty-driven returns, Diwali spike |
| Beauty & Personal Care | Low value, high volume, B2C-heavy, Instagram-driven, expiry dates, wedding/festive peaks |
| Home & Kitchen | Mixed slabs, festive spikes, bulky items with higher damage rates |
| Fashion & Apparel | Size/colour variants, high return rates, seasonal collections |
| Packaged Food & FMCG | 5–18% slabs, short shelf life, steady demand, distributor-heavy B2B |
| Industrial & MRO | B2B-only, long credit terms, field sales, low returns |
| Office & Stationery | Institutional B2B, academic-year and FY-end peaks |

Each pack defines: categories tree, fictional brand names, product-name patterns and variants, HSN codes + GST slabs, price bands and gross margin ranges, UoMs, shelf life / expiry, B2B/B2C mix, sales-channel and marketing-channel mix, monthly seasonality curve, return and defect rates, typical credit terms.

The user picks **one or more** industries; each becomes a business unit. Company name, HQ state and warehouse cities are also inputs. All names remain fictional. §1 above describes the **default** configuration.

### 8.4 Control Panel UI (D12) — new phase

**Recommendation: Streamlit**, as a thin layer over the same engine the CLI uses.

| Option | Verdict |
|---|---|
| **Streamlit** (recommended) | Pure Python, fast to build, good enough for forms, tables, progress bars and charts. Keeps the focus on data engineering |
| FastAPI + React | More "product-like", but roughly 3× the effort, mostly frontend work unrelated to the learning goals |
| Flask + templates | No real advantage over Streamlit here |

**Architecture**
- The UI never runs generation inside its own process. "Generate" launches the CLI command as a **background job** (subprocess).
- The engine writes progress to `generation_runs` (one row per run) and the new **`generation_run_steps`** table (one row per step: master data, procurement, sales, …) with `planned_rows`, `generated_rows`, `status`, `started_at`, `finished_at`, `rows_per_second`.
- The UI polls these tables every few seconds. Closing the browser doesn't stop the job; reopening shows the live state. CLI runs get the same history.

`generation_run_steps` — PK `generation_run_step_id`; FK `generation_run_id`; one run has many steps (1:N); populated by the engine at the start and end of each step and every N batches.

**Screens**
1. **Setup** — company name, industries (multi-select), HQ state, number of DCs and stores, date range (1–3 years), volume preset (Small / Medium / Large / Custom), scenario mix, random seed.
2. **Plan preview** — estimated row count per table and module, estimated database size, **estimated time** (row counts × measured throughput; calibrated on your machine after the first run).
3. **Run** — one-click Generate, overall and per-step progress bars, rows/sec, elapsed time and ETA, live log tail, Cancel.
4. **Data explorer** — actual vs planned row counts per table, sample rows, quick charts (orders by month, by channel, revenue by business unit).
5. **Validation** — data quality and reconciliation checks, pass/fail per check.
6. **DuckDB load** (after Phase 4) — load status per table, watermarks, history, errors.
7. **Anomaly simulation** (after Phase 7) — anomaly types, target tables, percentages.
8. **Reset** — with explicit confirmation.

### 8.5 Revised phase plan

| Phase | Scope | Change |
|---|---|---|
| 1 | Architecture & database design | Unchanged |
| 2 | Database implementation | Unchanged |
| 3 | Data generation engine (incl. industry packs, progress reporting) | Extended |
| 4 | DuckDB loader | Unchanged |
| **5** | **Control Panel UI** (Setup, Plan preview, Run, Explorer, Validation, Load status) | **New** |
| 6 | Testing & documentation | Was Phase 5 |
| 7 | Advanced DE simulation (CDC, anomalies) + anomaly screen in the UI | Was Phase 6 |

Tests are still written alongside every phase; Phase 6 is the consolidation and documentation pass.

---

## 9. Addendum A2 (2026-11-08): Workspaces (multiple companies) and Re-run Behaviour

| # | Decision | Choice |
|---|---|---|
| D13 | Multiple companies | One **workspace** per company: its own MySQL schemas and its own DuckDB file. Replaces the fixed names in D6 |
| D14 | Re-running for an existing company | Three explicit actions — **Continue**, **Rebuild**, **New workspace**. Nothing is ever silently appended or overwritten |

### 9.1 Workspaces (D13)

Generating data for company ABC, then DEF, creates two independent workspaces:

| Workspace | MySQL clean schema | MySQL simulation schema | DuckDB file |
|---|---|---|---|
| ABC (Beauty) | `scm_abc_oltp` | `scm_abc_sim` | `data/duckdb/abc.duckdb` |
| DEF (Electronics) | `scm_def_oltp` | `scm_def_sim` | `data/duckdb/def.duckdb` |

All workspaces share the same local MySQL server and the identical 92-table schema.

**Why one schema per company rather than one shared database with `company_id` on every table:**
- Each company really is a separate ERP instance; isolating them mirrors reality.
- Reset or rebuild one company without touching another.
- No `company_id` column threaded through 92 tables and every join, index and unique key.
- Loader, validation and dbt work unchanged — they only need to know which workspace.
- Trade-off: cross-company comparison needs cross-schema queries. That's easy in DuckDB (`ATTACH` several files).

**Registry:** a small control schema `scm_control` with one table, `workspaces` (PK `workspace_id`; workspace code, company name, industries, schema names, DuckDB path, base seed, config snapshot, status, created_at, updated_at). It's populated when a workspace is created. The UI lists it so you can pick a workspace; the CLI takes `--workspace abc`. `scm_control` is platform metadata, not part of any company's OLTP data.

### 9.2 Re-run behaviour (D14)

| Action | What happens | When to use |
|---|---|---|
| **Continue** (default for an existing workspace) | Continuous generation: the simulated clock moves forward from where the last run stopped. New orders, POs, payments, expenses reference existing customers/products; open documents progress (shipped → delivered, invoices get paid, overdue ones age). Document numbers carry on via `document_sequences`. History is never rewritten | Daily/weekly "the business kept running" — incremental loads and CDC practice |
| **Rebuild** | Drops and recreates the workspace's schemas, then regenerates history. Same config + same seed → **identical data**; new seed → different data with the same shape. Requires explicit confirmation. The DuckDB replica is marked stale and the next load is forced to a full reload | Config changed, or you want a clean restart |
| **New workspace** | Creates a separate company | Different company or industry |

Rules:
- **Continue never re-generates the past; Rebuild never appends.** That's what keeps duplicates out.
- Each run's seed is derived from the workspace base seed + run number, so a sequence of Continue runs is also reproducible.
- `generation_runs` records the simulated-clock window of every run; Continue starts from the last successful run's end. A failed or cancelled run is rolled back to that point before continuing.
- On Continue you may change volumes and scenario rates. You may **not** change industry or company identity — that requires Rebuild or a new workspace (otherwise the existing catalogue and history would contradict the new config).
- Each workspace carries a **build id** that changes on Rebuild. The DuckDB loader compares it with the id stored at its last load; on mismatch it refuses incremental loading and does a full reload. That prevents an old replica silently mixing with rebuilt source data.
