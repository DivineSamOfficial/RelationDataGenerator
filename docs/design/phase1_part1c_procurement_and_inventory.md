# Phase 1 – Part 1C: Procurement and Inventory Tables

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9). Baseline for Phase 2
**Last updated:** 2026-11-08
**Builds on:** Part 1A (entities, D1–D14), Part 1B (master data, D15–D24, conventions §2)

---

## 0. Scope

This part defines **24 tables**: 16 procurement and 8 inventory. It covers columns, keys, constraints, relationships, how each table is populated, the procurement and inventory flows, and the inventory valuation rules. It also resolves open questions Q1, Q2 (for inventory), Q7 and Q8, and two conflicts with the master prompt found while re-reading it (§1.3).

| Group | Tables |
|---|---|
| RFQ | `rfq_headers`, `rfq_line_items`, `rfq_suppliers`, `rfq_status_history` |
| Quotations | `quotation_headers`, `quotation_line_items`, `quotation_status_history` |
| Purchase orders | `purchase_order_headers`, `purchase_order_line_items`, `purchase_order_status_history` |
| Goods receipt | `goods_receipts`, `goods_receipt_line_items`, `goods_inspections` |
| Supplier billing | `supplier_invoices`, `supplier_invoice_line_items`, `supplier_payments` |
| Inventory | `inventory`, `inventory_transactions`, `inventory_batches`, `inventory_reservations`, `stock_adjustments`, `stock_adjustment_line_items`, `stock_transfers`, `stock_transfer_line_items` |

**The table count is unchanged (92).** One table changes **grain** (`inventory`, D26) and is explained in §1.2. Tables designed in later parts are referenced here by forward FKs: `shipment_line_items`, `invoice_line_items`, `order_line_items`, `sales_return_line_items`, `return_inspections` and `defective_stock` (Part 1D), and `supplier_payment_allocations` (Part 1E).

---

## 1. Decisions Made in This Part

### 1.1 New decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| D25 | Currency and tax regime (amends 1B) | Base currency is a **workspace setting** (`companies.base_currency_code`, default `INR`); every amount in a workspace is in that one currency. The tax model is **GST only** in v1, with rates, slabs and FY start configurable | The master prompt (§3) asks for configurable country, currency, tax and FY. Currency and FY are cheap to make configurable. A second tax regime (e.g. VAT) would change tax columns across ~20 tables, so v1 keeps GST and names tax columns generically where it costs nothing |
| D26 | `inventory` grain (**changes 1A**) | One row per **warehouse × product**, with a quantity column per stock status (`qty_sellable`, `qty_quarantine`, `qty_defective`), `qty_reserved`, the stock value, and the warehouse's reorder policy | Resolves Q1 and Q8. See §1.2 |
| D27 | Moving average cost (Q1) | Kept **per warehouse × product** on the `inventory` row. **Stock value is stored, and MAC = value ÷ quantity.** Every movement carries its unit cost and value change (§4) | Warehouses buy from different suppliers at different times, so their costs really do differ. Storing value instead of the rate avoids rounding drift: value always reconciles exactly to the GL |
| D28 | Source-document linking (Q2) | `inventory_transactions` has **one nullable FK per source document type** plus a CHECK that **exactly one** is set and that it matches the transaction type. Recommended for `journal_entries` too (confirmed in 1E) | Real FKs give integrity, DuckDB joins without CASE logic, and clean lineage in dbt. A polymorphic `source_type + source_id` can't be enforced and lets orphans in silently. The set of sources is small and stable (8) |
| D29 | Stock transfers between GSTINs (Q7) | Two GSTINs of the same company are "distinct persons" under GST, so a transfer between them is a **taxable supply**. With one GSTIN per state (D3), different GSTIN ⇔ different state ⇔ **IGST**. The `stock_transfers` row is the tax invoice: it carries `tax_invoice_number` from the source GSTIN's series, taxable value = moving-average cost, and IGST per line. Same-GSTIN transfers (DC → store in the same state) move under a delivery challan with no tax | Gets the real GST treatment without a separate transfer-invoice table. The source GSTIN pays output IGST and the destination claims the same amount as input IGST, so the company's net tax is zero. It's still recorded, so GST returns and the ledger show it |
| D30 | How POs arise | `po_source` = `RFQ_AWARD` (PO references the awarded quotation) or `DIRECT_REORDER` (routine repeat order to the preferred supplier, no RFQ). RFQs are awarded **per line**, so one RFQ can produce POs to several suppliers | The master prompt says POs reference the accepted quotation "where applicable". Its sample volumes (2,000 RFQs, 5,000 POs) also imply that many POs don't come from an RFQ |
| D31 | Receiving inspection | Inspection happens at the receiving dock on the receipt date. **Only accepted quantity enters stock** (SELLABLE). Rejected goods go back on the supplier's vehicle and never create a stock movement | Matches 1A §2.1 ("rejected quantity never enters stock") and keeps rejected goods out of the inventory value |
| D32 | Stored totals and counters | Header totals are stored on commercial and legal documents (quotation, PO, supplier invoice, transfer). Running quantity counters are stored on PO lines (received / accepted / rejected / invoiced / cancelled) and on reservations. Every stored total or counter has a reconciliation check against its source rows | A PO or invoice total is the agreed document amount, not just a sum. "Open PO quantity" is queried constantly by the replenishment engine. The checks stop these copies from drifting |
| D33 | Ledger immutability | `inventory_transactions` is **append-only**. A correction is a new row with `reversal_of_inventory_transaction_id` pointing at the original, followed by the correct row | Real ledgers work like this. It also gives clean insert-only incremental loads (Q5) and realistic "correction" events for CDC practice |
| D34 | Opening stock | Posted through a `stock_adjustments` document of type `OPENING_STOCK` on the first day of history, valued at each product's preferred-supplier reference cost | Every stock movement keeps a source document (D28). Matches D22 (opening balances only through documents and journals) |
| D35 | Supplier-invoice price differences | Stock is valued at the **PO price** when received. If the supplier invoice price differs, the difference goes to Purchase Price Variance (posting rules in 1E). It does **not** revalue stock after receipt | Revaluing stock that has already been partly sold is complex and error-prone. PPV is a standard, auditable ERP approach and makes a good analytics topic |
| D36 | Synthetic identifiers (Q9) | Every generated PAN starts with the fixed prefix `ZZX` (configurable); GSTINs stay fully valid (structure + check digit). Residual collision risk accepted and documented | Owner chose Option A: realistic, validatable identifiers that are easy to recognise as synthetic |

### 1.2 Why the `inventory` grain changes (D26)

Part 1A planned one row per **warehouse × product × stock status**. Working through valuation and reorder logic exposed three problems:

1. **Moving-average cost belongs to warehouse × product, not to a status.** A defective unit cost the same as a sellable one. Storing MAC on three status rows would mean three copies kept in sync.
2. **Reservations only apply to sellable stock.** "Available to promise" (sellable − reserved) would need two rows joined on every order.
3. **Reorder policy (Q8) is per warehouse × product.** It needs a home at exactly that grain.

The new grain fixes all three without adding a table. Each stock movement updates **one row**, so one row lock makes the update atomic.

**Impact:** Reconciliation becomes "sum of ledger quantity per warehouse × product × `stock_status` = the matching `qty_*` column". The ledger itself (`inventory_transactions`) still records the status of every movement, so analytics by status is unaffected. The only trade-off is that a fourth stock status would need a new column. The three statuses come from the business process (1A §2.3) and aren't expected to change.

### 1.3 Corrections to Part 1B (from the master prompt)

| Item | 1B said | Now | Change made |
|---|---|---|---|
| Currency | `companies.base_currency_code` CHECK `= 'INR'`; R26 "always INR" | Configurable per workspace (D25); CHECK becomes the format `^[A-Z]{3}$` | 1B §4.1 and R26 updated in the same commit |
| Real GSTINs (master prompt §3: "never use … actual GSTINs") | Valid-format GSTINs with correct check digits, from random fictional PANs | Option A chosen (D36): fixed synthetic PAN prefix `ZZX`, fully valid format and check digit | 1B §2.3 and V-MD-02 updated |

### 1.4 Open questions

| # | Question | Status |
|---|---|---|
| Q1 | Where moving-average cost lives | **Resolved:** D26/D27 |
| Q2 | Linking movements and journals to source documents | **Resolved for inventory:** D28. Journals confirmed in 1E |
| Q7 | GST on stock transfers between GSTINs | **Resolved:** D29 |
| Q8 | Per-warehouse reorder policy | **Resolved:** columns on `inventory` (D26) |
| Q9 | **Real-GSTIN guarantee.** About 15 million GSTINs are registered in India. ~1,500 random, structurally valid GSTINs carry a small but real chance (order of 10%) that at least one matches a real registration. **Option A (recommended):** keep everything valid (structure + check digit) but build every synthetic PAN from a fixed 3-letter prefix such as `ZZX` and document it, which lowers the risk without removing it. **Option B:** use a character that real GSTINs never contain in one fixed position (e.g. position 14 ≠ `Z`), which guarantees fiction but makes every GSTIN fail a strict format check. **Option C:** use a deliberately wrong check digit, which also guarantees fiction but fails checksum validation | **Resolved: Option A** (D36, owner decision 2026-11-08) |

---

## 2. Conventions Specific to Documents

These apply to every document table from this part onwards (procurement, inventory, sales, finance).

### 2.1 Document numbers

`<PREFIX>/<STATE>/<FY>/<SEQ6>`, e.g. `PO/KA/2026-27/000123`. The counter is held per document type × issuing GSTIN × financial year in `document_sequences` (R20, designed in 1G). `STATE` is the `state_code` of the issuing GSTIN, which is the warehouse's for warehouse documents and the HQ's for central ones. Each number is UNIQUE.

| Document | Prefix | Issuing GSTIN |
|---|---|---|
| RFQ | `RFQ` | Delivery warehouse |
| Quotation (internal reference) | `QTN` | Delivery warehouse of the RFQ |
| Purchase order | `PO` | Delivery warehouse |
| Goods receipt note | `GRN` | Receiving warehouse |
| Supplier invoice (internal reference) | `PINV` | Bill-to GSTIN |
| Supplier payment | `SPAY` | HQ (principal GSTIN) |
| Stock adjustment | `ADJ` | Warehouse |
| Stock transfer | `STO` | Source warehouse |
| Transfer tax invoice (inter-GSTIN only) | `STI` | Source warehouse |

### 2.2 Status-history tables (standard structure)

Every `<doc>_status_history` table has the same shape (1A §3):

| Column | Type | NN | Notes |
|---|---|---|---|
| `<doc>_status_history_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `<doc>_id` | as parent PK | ✓ | FK → header |
| `from_status` | `VARCHAR(30)` | | NULL for the first entry (document created) |
| `to_status` | `VARCHAR(30)` | ✓ | Same CHECK list as the header's status column |
| `changed_at` | `DATETIME(6)` | ✓ | Business time of the change (simulated clock) |
| `changed_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`; NULL = system / automatic |
| `change_reason` | `VARCHAR(255)` | | |

- **Constraints:** CHECK `from_status IS NULL OR from_status <> to_status`. Index `(<doc>_id, changed_at)`.
- **Behaviour:** Append-only. `updated_at = created_at`. A row is written in the same transaction as every status update on the header.
- **Rules (validation):** The chain is continuous: each `from_status` equals the previous `to_status`. The last `to_status` equals the header's current status. Only allowed transitions occur (§3 lists them per document).

### 2.3 Amount columns on taxable lines

Every taxable document line follows one pattern, so tax logic and tests can be reused:

| Column | Type | Meaning |
|---|---|---|
| `quantity` (role-named) | `DECIMAL(18,3)` | |
| `unit_price` | `DECIMAL(18,4)` | Excluding GST |
| `discount_percent` | `DECIMAL(7,4)` | 0–100 |
| `taxable_amount` | `DECIMAL(18,2)` | `ROUND(qty × unit_price × (1 − discount%/100), 2)` |
| `gst_rate` | `DECIMAL(7,4)` | Snapshot of the product's rate on the document date |
| `cgst_amount`, `sgst_amount` | `DECIMAL(18,2)` | Intra-state: each `ROUND(taxable × rate/2/100, 2)`. `sgst_amount` holds UTGST when the place of supply uses UTGST (1B `states.intra_state_tax_component`) |
| `igst_amount` | `DECIMAL(18,2)` | Inter-state: `ROUND(taxable × rate/100, 2)` |
| `line_total` | `DECIMAL(18,2)` | `taxable + cgst + sgst + igst` |

- **Constraints on every such line:** CHECK `(cgst_amount = 0 AND sgst_amount = 0) OR igst_amount = 0`; CHECK `cgst_amount = sgst_amount`; CHECK `line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount`; CHECK all amounts `>= 0`; CHECK `discount_percent BETWEEN 0 AND 100`.
- **Headers carry** `taxable_amount`, `cgst_amount`, `sgst_amount`, `igst_amount`, `round_off_amount` (`DECIMAL(18,2)`, between −0.50 and +0.50) and `total_amount`. Each total equals the sum of its lines; the header total = sum of line totals + round-off, rounded to the whole rupee (V-PR-01).
- Whether a document is intra- or inter-state is stored once on the header as `is_inter_state` (a snapshot, because the supplier's address can change later).

---

## 3. Procurement Tables

### 3.0 Procure-to-pay flow and timing

```
replenishment check (daily, per warehouse)
  ├─ routine & preferred supplier known ─────────────► PO (DIRECT_REORDER)
  └─ otherwise ─► RFQ ─► invite 2–5 suppliers ─► quotations ─► evaluate ─► award per line ─► PO(s) (RFQ_AWARD)
PO approved ─► sent ─► 1..n deliveries ─► GRN + dock inspection ─► accepted qty → inventory (PURCHASE_RECEIPT)
                                                     └─ rejected qty → back to supplier (no stock movement)
supplier invoice (three-way match: PO price × accepted qty) ─► AP open item (1E) ─► supplier payment ─► allocation (1E)
```

Typical timing (config defaults): RFQ response window 3–7 days; evaluation 1–3 days; PO approval 0–2 days; delivery after the quoted lead time ± the supplier's reliability (late in ~15% of POs, worse for low-rated suppliers); invoice 0–5 days after receipt; payment on the due date ± behaviour (late in ~20%, MSME suppliers within 45 days).

---

#### `rfq_headers`

| Column | Type | NN | Notes |
|---|---|---|---|
| `rfq_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `rfq_number` | `VARCHAR(30)` | ✓ | UNIQUE |
| `rfq_date` | `DATE` | ✓ | Date issued to suppliers |
| `rfq_type` | `VARCHAR(30)` | ✓ | `REPLENISHMENT` / `NEW_PRODUCT` / `SEASONAL_BUILD` / `BULK_DEMAND` |
| `delivery_warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` (DCs only) |
| `business_unit_id` | `SMALLINT UNSIGNED` | ✓ | FK → `business_units`. An RFQ covers products of one BU |
| `response_due_date` | `DATE` | ✓ | |
| `required_by_date` | `DATE` | ✓ | When the goods are needed at the warehouse |
| `rfq_status` | `VARCHAR(30)` | ✓ | `DRAFT` / `ISSUED` / `UNDER_EVALUATION` / `AWARDED` / `PARTIALLY_AWARDED` / `NO_AWARD` / `CANCELLED` |
| `created_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` (Purchasing) |
| `awarded_at` | `DATETIME(6)` | | |
| `cancellation_reason` | `VARCHAR(255)` | | |

- **Constraints:** CHECK lists; CHECK `response_due_date >= rfq_date`; CHECK `required_by_date >= response_due_date`; CHECK `(rfq_status IN ('AWARDED','PARTIALLY_AWARDED')) = (awarded_at IS NOT NULL)`; CHECK `(rfq_status = 'CANCELLED') = (cancellation_reason IS NOT NULL)`.
- **Indexes:** `(rfq_status, rfq_date)`, `(delivery_warehouse_id, rfq_date)`.
- **Transitions:** DRAFT → ISSUED → UNDER_EVALUATION → AWARDED / PARTIALLY_AWARDED / NO_AWARD; DRAFT or ISSUED → CANCELLED.
- **Populated:** By the replenishment engine (§3.0) for products below reorder level that aren't eligible for direct reorder, plus seasonal builds before festive peaks (from pack seasonality) and new-product launches. Products are grouped by warehouse × BU into one RFQ (1–8 lines). `NO_AWARD` happens when no acceptable quotation arrives; the engine then re-issues a new RFQ.

#### `rfq_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `rfq_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `rfq_id` | `BIGINT UNSIGNED` | ✓ | FK → `rfq_headers` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `requested_quantity` | `DECIMAL(18,3)` | ✓ | |
| `target_unit_price` | `DECIMAL(18,4)` | | Internal budget price (not shown to suppliers) |
| `line_status` | `VARCHAR(30)` | ✓ | `OPEN` / `AWARDED` / `NOT_AWARDED` / `CANCELLED` |

- **Keys:** UNIQUE `(rfq_id, line_number)`; UNIQUE `(rfq_id, product_id)`.
- **Constraints:** CHECK `requested_quantity > 0`; CHECK list. The product must belong to the RFQ's BU (V-PR-03).

#### `rfq_suppliers`
Suppliers invited to an RFQ (M:N RFQ ↔ supplier).

| Column | Type | NN | Notes |
|---|---|---|---|
| `rfq_supplier_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `rfq_id` | `BIGINT UNSIGNED` | ✓ | FK → `rfq_headers` |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `invited_date` | `DATE` | ✓ | = `rfq_date` |
| `response_status` | `VARCHAR(30)` | ✓ | `INVITED` / `QUOTED` / `DECLINED` / `NO_RESPONSE` |
| `responded_date` | `DATE` | | |
| `decline_reason` | `VARCHAR(100)` | | "Capacity full", "Product discontinued"… |

- **Keys:** UNIQUE `(rfq_id, supplier_id)`.
- **Constraints:** CHECK list; CHECK `(response_status IN ('QUOTED','DECLINED')) = (responded_date IS NOT NULL)`; CHECK `(response_status = 'DECLINED') = (decline_reason IS NOT NULL)`.
- **Populated:** 2–5 active approved suppliers per RFQ, chosen from `supplier_products` for the RFQ's products (preferred supplier always included). Each supplier is approved for at least one RFQ line (V-PR-04). Response mix (default): ~70% QUOTED, ~15% DECLINED, ~15% NO_RESPONSE. `NO_RESPONSE` is set when the response due date passes.

#### `rfq_status_history`
Standard structure (§2.2), FK `rfq_id`.

#### `quotation_headers`
A supplier's quotation in response to an RFQ invitation.

| Column | Type | NN | Notes |
|---|---|---|---|
| `quotation_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `quotation_number` | `VARCHAR(30)` | ✓ | UNIQUE. Internal reference |
| `rfq_supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `rfq_suppliers`, UNIQUE. One quotation per invitation; identifies both the RFQ and the supplier |
| `supplier_quotation_reference` | `VARCHAR(40)` | ✓ | The supplier's own number |
| `quotation_date` | `DATE` | ✓ | Between `rfq_date` and `response_due_date` |
| `valid_until_date` | `DATE` | ✓ | |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_terms`. Offered terms |
| `is_inter_state` | `BOOLEAN` | ✓ | Supplier state ≠ delivery warehouse state |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | Header totals (§2.3) |
| `evaluation_score` | `DECIMAL(5,2)` | | 0–100, set during evaluation |
| `evaluated_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` |
| `evaluated_date` | `DATE` | | |
| `quotation_status` | `VARCHAR(30)` | ✓ | `RECEIVED` / `UNDER_EVALUATION` / `ACCEPTED` / `PARTIALLY_ACCEPTED` / `REJECTED` / `EXPIRED` / `WITHDRAWN` |
| `rejection_reason` | `VARCHAR(100)` | | `PRICE_TOO_HIGH`, `LEAD_TIME_TOO_LONG`, `LOWER_RATED`, `INCOMPLETE` … |

- **Constraints:** CHECK `valid_until_date >= quotation_date`; CHECK `evaluation_score BETWEEN 0 AND 100`; CHECK `(evaluation_score IS NULL) = (evaluated_date IS NULL)`; CHECK `(quotation_status = 'REJECTED') = (rejection_reason IS NOT NULL)`; CHECK list.
- **Transitions:** RECEIVED → UNDER_EVALUATION → ACCEPTED / PARTIALLY_ACCEPTED / REJECTED; RECEIVED → WITHDRAWN; UNDER_EVALUATION → EXPIRED (validity lapsed before award).
- **Rules:** The parent `rfq_suppliers.response_status` is `QUOTED`. ACCEPTED ⇔ all lines awarded; PARTIALLY_ACCEPTED ⇔ some (V-PR-05).
- **Populated:** One per QUOTED invitation. Score = weighted price (60%), lead time (25%) and supplier rating (15%), configurable.

#### `quotation_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `quotation_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `quotation_id` | `BIGINT UNSIGNED` | ✓ | FK → `quotation_headers` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `rfq_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `rfq_line_items`. The product comes from the RFQ line |
| `quoted_quantity` | `DECIMAL(18,3)` | ✓ | May be less than requested (supplier capacity) |
| `unit_price` … `line_total` | | ✓ | Standard amount pattern (§2.3) |
| `lead_time_days` | `SMALLINT UNSIGNED` | ✓ | |
| `is_awarded` | `BOOLEAN` | ✓ | Default FALSE |
| `awarded_rfq_line_key` | generated `BIGINT UNSIGNED` | | `IF(is_awarded, rfq_line_item_id, NULL)` STORED |

- **Keys:** UNIQUE `(quotation_id, line_number)`; UNIQUE `(quotation_id, rfq_line_item_id)`; UNIQUE `(awarded_rfq_line_key)`: each RFQ line is awarded to at most one quotation line (1B §2.4b).
- **Constraints:** CHECK `quoted_quantity > 0`; CHECK `lead_time_days BETWEEN 1 AND 120`; amount CHECKs (§2.3).
- **Rules:** The RFQ line belongs to the same RFQ as the quotation (V-PR-06). Quoted quantity ≤ requested quantity.
- **Populated:** A supplier quotes the RFQ lines it's approved for (sometimes fewer). Price = `supplier_products.reference_unit_cost` × random factor (0.92–1.10); lead time = reference ± 30%. The award goes to the best-scoring line per RFQ line.

#### `quotation_status_history`
Standard structure (§2.2), FK `quotation_id`.

#### `purchase_order_headers`

| Column | Type | NN | Notes |
|---|---|---|---|
| `purchase_order_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `po_number` | `VARCHAR(30)` | ✓ | UNIQUE |
| `po_date` | `DATE` | ✓ | |
| `po_source` | `VARCHAR(30)` | ✓ | `RFQ_AWARD` / `DIRECT_REORDER` (D30) |
| `quotation_id` | `BIGINT UNSIGNED` | | FK → `quotation_headers`. Set iff `RFQ_AWARD` |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `supplier_address_id` | `BIGINT UNSIGNED` | ✓ | FK → `supplier_addresses`. Dispatch-from address valid on `po_date` (snapshot) |
| `delivery_warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` (DCs). Part of a UNIQUE used by GRNs |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_terms`. Snapshot from the quotation or supplier |
| `is_inter_state` | `BOOLEAN` | ✓ | |
| `expected_delivery_date` | `DATE` | ✓ | Latest expected date across lines |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | §2.3 |
| `po_status` | `VARCHAR(30)` | ✓ | See transitions |
| `created_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`; `approval_limit >= total_amount` |
| `approved_at` | `DATETIME(6)` | | |
| `cancellation_reason` | `VARCHAR(255)` | | Also used for short-close reason |

- **Keys:** UNIQUE `(quotation_id)`: one PO per awarded quotation (MySQL allows many NULLs, so direct reorders are unaffected). UNIQUE `(purchase_order_id, delivery_warehouse_id)`, the target of the composite FK from `goods_receipts`.
- **Constraints:** CHECK `(po_source = 'RFQ_AWARD') = (quotation_id IS NOT NULL)`; CHECK `(approved_by_employee_id IS NULL) = (approved_at IS NULL)`; CHECK `po_status IN ('DRAFT','PENDING_APPROVAL') OR approved_at IS NOT NULL` (anything past approval has been approved); CHECK `(po_status IN ('CANCELLED','SHORT_CLOSED')) = (cancellation_reason IS NOT NULL)`; CHECK `expected_delivery_date >= po_date`.
- **Statuses and transitions:** `DRAFT` → `PENDING_APPROVAL` → `APPROVED` → `SENT` → `PARTIALLY_RECEIVED` → `RECEIVED` → `CLOSED` (fully received and invoiced). `DRAFT`/`PENDING_APPROVAL`/`APPROVED`/`SENT` → `CANCELLED` (before any receipt). `PARTIALLY_RECEIVED` → `SHORT_CLOSED` (supplier can't deliver the rest; that quantity is cancelled), then → `CLOSED` once invoicing is complete.
- **Rules:** The PO supplier equals the quotation's supplier (V-PR-07). The supplier address belongs to the supplier and was valid on `po_date`. The approver was active on the approval date.
- **Indexes:** `(supplier_id, po_date)`, `(po_status, expected_delivery_date)`, `(quotation_id)`.
- **Populated:** One PO per awarded quotation (RFQ path), or one per supplier per replenishment run (direct path: products whose preferred supplier was bought from in the last 180 days and whose order value is under a threshold). Scenario defaults: ~2% cancelled, ~4% short-closed.

#### `purchase_order_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `purchase_order_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `purchase_order_id` | `BIGINT UNSIGNED` | ✓ | FK → `purchase_order_headers` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `quotation_line_item_id` | `BIGINT UNSIGNED` | | FK → `quotation_line_items`, UNIQUE. The awarded line (RFQ path) |
| `ordered_quantity` | `DECIMAL(18,3)` | ✓ | |
| `unit_price` … `line_total` | | ✓ | §2.3. Price from the quotation, or reference cost for direct reorders |
| `expected_delivery_date` | `DATE` | ✓ | `po_date` + lead time |
| `quantity_received` | `DECIMAL(18,3)` | ✓ | Default 0. Sum of GRN line quantities |
| `quantity_accepted` | `DECIMAL(18,3)` | ✓ | Default 0. Sum of inspection accepted qty |
| `quantity_rejected` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `quantity_invoiced` | `DECIMAL(18,3)` | ✓ | Default 0. Sum of supplier invoice line quantities |
| `quantity_cancelled` | `DECIMAL(18,3)` | ✓ | Default 0. Set on short-close or cancellation |
| `quantity_open` | generated `DECIMAL(18,3)` | | `ordered − accepted − cancelled` STORED. What's still expected from the supplier |
| `line_status` | `VARCHAR(30)` | ✓ | `OPEN` / `PARTIALLY_RECEIVED` / `RECEIVED` / `CANCELLED` / `CLOSED` |

- **Keys:** UNIQUE `(purchase_order_id, line_number)`; UNIQUE `(purchase_order_id, product_id)`.
- **Constraints:** CHECK `ordered_quantity > 0`; CHECK `quantity_accepted + quantity_rejected <= quantity_received`; CHECK `quantity_received <= ordered_quantity + quantity_rejected` (rejected goods may be re-sent); CHECK `quantity_accepted + quantity_cancelled <= ordered_quantity`; CHECK `quantity_invoiced <= quantity_accepted`; CHECK all quantities `>= 0`.
- **Rules:** Counters equal the sums of their source rows (V-PR-08, D32). For the RFQ path, the product equals the quotation line's RFQ-line product, and ordered quantity ≤ quoted quantity.
- **Indexes:** `(product_id, line_status)`. The replenishment engine reads open PO quantity per product × warehouse.

#### `purchase_order_status_history`
Standard structure (§2.2), FK `purchase_order_id`.

#### `goods_receipts`
Goods receipt note (GRN), one per supplier delivery.

| Column | Type | NN | Notes |
|---|---|---|---|
| `goods_receipt_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `grn_number` | `VARCHAR(30)` | ✓ | UNIQUE |
| `purchase_order_id` | `BIGINT UNSIGNED` | ✓ | Composite FK (below) |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | Composite FK. Always the PO's delivery warehouse |
| `receipt_date` | `DATE` | ✓ | |
| `received_at` | `DATETIME(6)` | ✓ | Time at the dock |
| `supplier_delivery_note_number` | `VARCHAR(40)` | ✓ | |
| `eway_bill_number` | `CHAR(12)` | | Required by GST for goods consignments over ₹50,000 |
| `vehicle_number` | `VARCHAR(15)` | | Fictional registration (`KA01AB1234` format) |
| `received_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` (that warehouse) |
| `grn_status` | `VARCHAR(30)` | ✓ | `RECEIVED` / `INSPECTED` / `CANCELLED` |

- **Keys:** Composite FK `(purchase_order_id, warehouse_id)` → `purchase_order_headers (purchase_order_id, delivery_warehouse_id)`. The database guarantees goods are received only at the warehouse they were ordered for.
- **Constraints:** CHECK list; CHECK `eway_bill_number REGEXP '^[0-9]{12}$'`; CHECK `DATE(received_at) = receipt_date`.
- **Rules:** `receipt_date >= po_date` and the PO was SENT or later (V-PR-09). Late delivery = `receipt_date >` line `expected_delivery_date`, so lateness is derived, not stored.
- **Populated:** Each PO line is delivered in 1–3 deliveries (partial deliveries in ~20% of POs). A delivery usually covers several lines, giving one GRN per delivery. Delay is drawn from the supplier's rating.

#### `goods_receipt_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `goods_receipt_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `goods_receipt_id` | `BIGINT UNSIGNED` | ✓ | FK → `goods_receipts` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `purchase_order_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `purchase_order_line_items` |
| `quantity_received` | `DECIMAL(18,3)` | ✓ | Physically received (before inspection) |
| `receiving_location_id` | `BIGINT UNSIGNED` | ✓ | FK → `warehouse_locations` (zone RECEIVING) |
| `supplier_batch_number` | `VARCHAR(40)` | | For batch-tracked products |
| `manufacture_date` | `DATE` | | |
| `expiry_date` | `DATE` | | Required for products with expiry |

- **Keys:** UNIQUE `(goods_receipt_id, line_number)`; UNIQUE `(goods_receipt_id, purchase_order_line_item_id)`.
- **Constraints:** CHECK `quantity_received > 0`; CHECK `expiry_date IS NULL OR manufacture_date IS NULL OR expiry_date > manufacture_date`.
- **Rules:** The PO line belongs to the GRN's PO (V-PR-10). The location belongs to the GRN warehouse. For expiry products, remaining shelf life on receipt ≥ the pack's minimum (e.g. 60%); otherwise the generator rejects the line at inspection with reason `SHORT_SHELF_LIFE`.

#### `goods_inspections`
Dock inspection result for a GRN line (1 : 1 with the GRN line). Kept as its own table because it's a separate event, with its own actor, time and outcome.

| Column | Type | NN | Notes |
|---|---|---|---|
| `goods_inspection_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `goods_receipt_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `goods_receipt_line_items`, UNIQUE |
| `inspected_at` | `DATETIME(6)` | ✓ | Same day as receipt (D31) |
| `inspected_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `quantity_accepted` | `DECIMAL(18,3)` | ✓ | Enters SELLABLE stock |
| `quantity_rejected` | `DECIMAL(18,3)` | ✓ | Returned to the supplier at the door |
| `inspection_result` | `VARCHAR(30)` | ✓ | `ACCEPTED` / `PARTIALLY_ACCEPTED` / `REJECTED` |
| `rejection_reason` | `VARCHAR(30)` | | `DAMAGED_IN_TRANSIT` / `WRONG_ITEM` / `QUALITY_FAILED` / `SHORT_SHELF_LIFE` / `EXCESS_QUANTITY` |
| `inspection_notes` | `VARCHAR(255)` | | |

- **Constraints:** CHECK quantities `>= 0`; CHECK `(quantity_rejected > 0) = (rejection_reason IS NOT NULL)`; CHECK result consistent with the quantities (`ACCEPTED` ⇔ rejected = 0; `REJECTED` ⇔ accepted = 0).
- **Rules:** `accepted + rejected = GRN line quantity_received` (V-PR-11).
- **Populated:** Rejection on ~3% of GRN lines (configurable, higher for low-rated suppliers and fragile categories). Completing the inspection, in the same transaction: inserts the batch (if batch-tracked) and the `PURCHASE_RECEIPT` inventory transaction for the accepted quantity; updates `inventory` and the PO line counters; sets `grn_status = INSPECTED`.

#### `supplier_invoices`
The supplier's tax invoice (bill), matched to the PO and receipts.

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_invoice_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `internal_reference` | `VARCHAR(30)` | ✓ | UNIQUE (`PINV/...`) |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `supplier_invoice_number` | `VARCHAR(40)` | ✓ | The supplier's number |
| `purchase_order_id` | `BIGINT UNSIGNED` | ✓ | FK → `purchase_order_headers`. One PO per invoice |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK. Bill-to GSTIN (snapshot), decides where input credit is claimed |
| `supplier_gstin` | `CHAR(15)` | | Snapshot. NULL for unregistered suppliers |
| `invoice_date` | `DATE` | ✓ | |
| `received_date` | `DATE` | ✓ | Date the bill reached Accounts |
| `due_date` | `DATE` | ✓ | `invoice_date + payment_terms.due_days` |
| `is_inter_state` | `BOOLEAN` | ✓ | |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | §2.3 including `round_off_amount` |
| `match_status` | `VARCHAR(30)` | ✓ | `MATCHED` / `PRICE_VARIANCE` / `QUANTITY_HOLD` |
| `invoice_status` | `VARCHAR(30)` | ✓ | `RECEIVED` / `ON_HOLD` / `APPROVED` / `CANCELLED` |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (Finance) |
| `approved_at` | `DATETIME(6)` | | |

- **Keys:** UNIQUE `(supplier_id, supplier_invoice_number)`. The same supplier can't bill the same number twice; this is a duplicate-invoice guard for the clean schema, while anomaly mode may simulate duplicates in `scm_<code>_sim`.
- **Constraints:** GSTIN format; CHECK `received_date >= invoice_date`; CHECK `due_date >= invoice_date`; CHECK lists; CHECK `(invoice_status = 'APPROVED') = (approved_at IS NOT NULL)`; CHECK `match_status <> 'QUANTITY_HOLD' OR invoice_status <> 'APPROVED'`.
- **Rules:** The PO's supplier is this supplier and its delivery warehouse's GSTIN is the bill-to GSTIN (V-PR-12). For unregistered or composition suppliers all tax amounts are 0. Paid status isn't stored here: it lives in `accounts_payable` (1E), so there's only one source of truth for outstanding amounts.
- **Three-way match (per line, cumulative):** invoiced qty ≤ accepted qty not yet invoiced (otherwise `QUANTITY_HOLD`, held until goods arrive); unit price within tolerance of the PO price (otherwise `PRICE_VARIANCE`, approved with variance → PPV, D35).
- **Populated:** Usually one invoice per GRN, dated 0–5 days after receipt (sometimes one per PO after full receipt). Price variance on ~5% of invoices (small ± differences, a legitimate business event, not an anomaly). Approved by Finance within a few days, which creates the AP open item and the journal (1E).

#### `supplier_invoice_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_invoice_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_invoice_id` | `BIGINT UNSIGNED` | ✓ | FK → `supplier_invoices` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `purchase_order_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `purchase_order_line_items` |
| `invoiced_quantity` | `DECIMAL(18,3)` | ✓ | |
| `unit_price` … `line_total` | | ✓ | §2.3, as billed |
| `po_unit_price` | `DECIMAL(18,4)` | ✓ | Snapshot of the PO line price at match time. Price variance = `(unit_price − po_unit_price) × qty` |

- **Keys:** UNIQUE `(supplier_invoice_id, line_number)`; UNIQUE `(supplier_invoice_id, purchase_order_line_item_id)`.
- **Constraints:** CHECK `invoiced_quantity > 0`; amount CHECKs.
- **Rules:** The PO line belongs to the invoice's PO (V-PR-13). Cumulative invoiced qty per PO line ≤ accepted qty (V-PR-08). The GST rate equals the PO line's rate.

#### `supplier_payments`
Money paid to a supplier. Allocation to invoices is in `supplier_payment_allocations` (1E).

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_payment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `payment_number` | `VARCHAR(30)` | ✓ | UNIQUE |
| `supplier_id` | `BIGINT UNSIGNED` | ✓ | FK → `suppliers` |
| `payment_date` | `DATE` | ✓ | |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK → `bank_accounts` (PAYMENTS or GENERAL, never petty cash) |
| `payment_method_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_methods` (`usable_for_payments`) |
| `amount` | `DECIMAL(18,2)` | ✓ | |
| `payment_type` | `VARCHAR(30)` | ✓ | `AGAINST_INVOICE` / `ADVANCE` |
| `bank_reference` | `VARCHAR(40)` | | UTR / cheque number; set once processed |
| `payment_status` | `VARCHAR(30)` | ✓ | `INITIATED` / `COMPLETED` / `FAILED` / `REVERSED` |
| `failure_reason` | `VARCHAR(100)` | | |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `processed_at` | `DATETIME(6)` | | |

- **Constraints:** CHECK `amount > 0`; CHECK lists; CHECK `(payment_status = 'FAILED') = (failure_reason IS NOT NULL)`; CHECK `payment_status = 'INITIATED' OR processed_at IS NOT NULL`.
- **Rules:** Only `COMPLETED` payments are allocated (mirrors D5). The amount respects the payment method's min/max. The approver's limit ≥ amount.
- **Indexes:** `(supplier_id, payment_date)`, `(payment_status)`.
- **Populated:** A weekly payment run (configurable weekday) picks approved invoices that will be due before the next run. Scenario defaults: ~20% paid late (cash-flow squeeze; never MSME beyond 45 days), ~1% FAILED (retried in the next run), occasional ADVANCE for new suppliers. Paying several invoices with one payment is normal.

---

## 4. Inventory Valuation Rules (moving weighted average, D1/D27)

Notation for one `inventory` row (warehouse W × product P): `Q` = on-hand quantity (sellable + quarantine + defective), `V` = stock value, `MAC = V / Q`.

| Movement | Unit cost used | Effect |
|---|---|---|
| Purchase receipt (accepted qty) | PO line net unit price (`taxable_amount / qty`); GST is not cost when input credit is claimed | `V += q × cost`; MAC recalculated |
| Sale issue | Current MAC (snapshot on the ledger row → COGS) | `V −= ROUND(q × MAC, 2)`; MAC unchanged |
| Customer return receipt | The unit cost of the original sale issue (reverses that COGS) | `V += q × original cost`; MAC recalculated |
| Status change (Q → S, Q → D, S → D, D → S) | MAC | Value unchanged; quantity moves between buckets |
| Transfer out | Source MAC | Source `V −= q × MAC`; value sits in Inventory in Transit |
| Transfer in | The transfer line's unit cost (source MAC at dispatch) | Destination `V += q × cost`; destination MAC recalculated |
| Adjustment out (shrinkage, damage write-off, expiry, disposal) | MAC | `V −= q × MAC` → write-off expense |
| Adjustment in (found stock) | MAC (or the reference cost if Q = 0) | `V += q × cost` → adjustment gain |
| Opening stock (D34) | Preferred supplier reference cost | Sets the initial V and MAC |

- **Rounding:** Values are rounded to 2 decimals per movement and MAC is shown to 4. When an issue brings Q to exactly 0, the issue value is set to the remaining V, so V becomes exactly 0 (no residual paise).
- **When Q = 0:** MAC is undefined. `inventory.last_unit_cost` keeps the last known cost for reference and for found-stock valuation.
- **Defective stock** stays valued at MAC until disposed of (write-off, scrap or return to supplier). Disposal is the moment the loss is recognised. This keeps inventory value = GL Inventory at all times.
- **GL reconciliation (1E):** GL Inventory = Σ `inventory.stock_value`; GL Inventory in Transit = Σ over open transfers of (shipped − received good − received damaged − lost) × unit cost.

---

## 5. Inventory Tables

### 5.0 Movement map (ledger row types)

| `transaction_type` | Sign | `stock_status` | Source FK (D28) | Caused by |
|---|---|---|---|---|
| `PURCHASE_RECEIPT` | + | SELLABLE | `goods_receipt_line_item_id` | Dock inspection accepted |
| `SALES_ISSUE` | − | SELLABLE | `shipment_line_item_id` | Dispatch of a shipment |
| `SALES_ISSUE` | − | SELLABLE | `invoice_line_item_id` | Counter sale / store pickup handover (no shipment, A1 §8.2) |
| `RETURN_RECEIPT` | + | QUARANTINE | `sales_return_line_item_id` | Customer return arrives |
| `RETURN_DISPOSITION` | − then + | QUARANTINE → SELLABLE or DEFECTIVE | `return_inspection_id` | Return inspection outcome (2 rows) |
| `TRANSFER_OUT` | − | SELLABLE | `stock_transfer_line_item_id` | Transfer dispatched |
| `TRANSFER_IN` | + | SELLABLE or DEFECTIVE | `stock_transfer_line_item_id` | Transfer received (damaged in transit → DEFECTIVE) |
| `ADJUSTMENT` | ± | any | `stock_adjustment_line_item_id` | Cycle count, damage reclass, shrinkage, found stock, expiry write-off, opening stock |
| `DEFECTIVE_DISPOSITION` | − (or −D / +S for repair) | DEFECTIVE | `defective_stock_id` | Scrap, return to supplier, repair (register designed in 1D) |

Reservations are **not** movements. They change `inventory.qty_reserved` only (§5 `inventory_reservations`).

#### `inventory`
Current balance and policy per warehouse × product (D26).

| Column | Type | NN | Notes |
|---|---|---|---|
| `inventory_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `qty_sellable` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `qty_quarantine` | `DECIMAL(18,3)` | ✓ | Default 0. Returns awaiting inspection |
| `qty_defective` | `DECIMAL(18,3)` | ✓ | Default 0. Awaiting disposition |
| `qty_reserved` | `DECIMAL(18,3)` | ✓ | Default 0. Sellable stock held for orders |
| `qty_on_hand` | generated | | `qty_sellable + qty_quarantine + qty_defective` STORED |
| `qty_available` | generated | | `qty_sellable − qty_reserved` STORED. Available to promise |
| `stock_value` | `DECIMAL(18,2)` | ✓ | Default 0. Authoritative value (D27) |
| `moving_average_cost` | generated `DECIMAL(18,4)` | | `IF(qty_on_hand > 0, stock_value / qty_on_hand, NULL)` STORED |
| `last_unit_cost` | `DECIMAL(18,4)` | | Last non-null MAC |
| `reorder_level` | `DECIMAL(18,3)` | ✓ | Warehouse policy (Q8); initialised from `products.reorder_level` × the warehouse's demand share |
| `reorder_quantity` | `DECIMAL(18,3)` | ✓ | |
| `max_stock_level` | `DECIMAL(18,3)` | ✓ | Cap for order quantity and transfers |
| `is_stocked` | `BOOLEAN` | ✓ | Whether this warehouse ranges the product (stores carry a subset) |
| `last_receipt_at` | `DATETIME(6)` | | |
| `last_issue_at` | `DATETIME(6)` | | |
| `last_counted_at` | `DATETIME(6)` | | Last cycle count |

- **Keys:** UNIQUE `(warehouse_id, product_id)`.
- **Constraints:** CHECK every `qty_*` `>= 0` (**inventory never negative**, enforced by the database); CHECK `qty_reserved <= qty_sellable`; CHECK `stock_value >= 0`; CHECK `(qty_sellable + qty_quarantine + qty_defective = 0) = (stock_value = 0)`; CHECK `reorder_level >= 0 AND reorder_quantity > 0 AND max_stock_level >= reorder_level + reorder_quantity`. The value check spells out the sum instead of using the generated `qty_on_hand`, so it doesn't depend on how MySQL orders generated-column and CHECK evaluation. Phase 2 tests every CHECK here with deliberately bad rows.
- **Indexes:** `(product_id)`; `(warehouse_id, is_stocked)`. The replenishment query compares `qty_available` + open PO qty with `reorder_level`, so Phase 2 may add an index on `(warehouse_id, qty_available)`.
- **Relationships:** N : 1 `warehouses`, `products`. Reconciles to `inventory_transactions` (quantities and value) and `inventory_reservations` (reserved).
- **Concurrency:** Each movement updates its row with `SELECT … FOR UPDATE` inside the same transaction as the ledger insert, so the balance and ledger can't diverge. Generation is single-writer by design, but the rule holds for any writer.
- **Populated:** One row per warehouse × ranged product, created with the opening-stock adjustment (D34). DCs range all active products of their BUs; stores range the B2C-relevant subset from the pack. Updated by every movement and reservation. This is a **mutable** table: the loader treats it as a snapshot (`updated_at` watermark), while the ledger is the history.

#### `inventory_transactions`
The single inventory movement ledger (R2/R3, D28, D33).

| Column | Type | NN | Notes |
|---|---|---|---|
| `inventory_transaction_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `transaction_at` | `DATETIME(6)` | ✓ | Business time of the movement |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `inventory_batch_id` | `BIGINT UNSIGNED` | | FK → `inventory_batches`. Required for batch-tracked products |
| `warehouse_location_id` | `BIGINT UNSIGNED` | | FK → `warehouse_locations` |
| `transaction_type` | `VARCHAR(30)` | ✓ | §5.0 |
| `stock_status` | `VARCHAR(30)` | ✓ | `SELLABLE` / `QUARANTINE` / `DEFECTIVE`: the bucket this row changes |
| `quantity` | `DECIMAL(18,3)` | ✓ | Signed, never 0 |
| `unit_cost` | `DECIMAL(18,4)` | ✓ | §4 |
| `value_change` | `DECIMAL(18,2)` | ✓ | Signed. Usually `ROUND(quantity × unit_cost, 2)`; the closing issue takes the remaining value (§4) |
| `goods_receipt_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `shipment_line_item_id` | `BIGINT UNSIGNED` | | Source FK (1D) |
| `invoice_line_item_id` | `BIGINT UNSIGNED` | | Source FK (1D), handover sales |
| `sales_return_line_item_id` | `BIGINT UNSIGNED` | | Source FK (1D) |
| `return_inspection_id` | `BIGINT UNSIGNED` | | Source FK (1D) |
| `stock_transfer_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `stock_adjustment_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `defective_stock_id` | `BIGINT UNSIGNED` | | Source FK (1D) |
| `reversal_of_inventory_transaction_id` | `BIGINT UNSIGNED` | | FK → self, UNIQUE. Set on correction rows (D33) |
| `generation_run_id` | `BIGINT UNSIGNED` | ✓ | FK → `generation_runs` (1G). Which run wrote it. Supports Continue/rollback (D14) |

- **Constraints:**
  - CHECK `quantity <> 0`; CHECK `quantity * value_change >= 0` (value moves in the same direction as quantity); CHECK `unit_cost >= 0`.
  - CHECK lists for type and status.
  - **Exactly one source:** CHECK `(goods_receipt_line_item_id IS NOT NULL) + (shipment_line_item_id IS NOT NULL) + (invoice_line_item_id IS NOT NULL) + (sales_return_line_item_id IS NOT NULL) + (return_inspection_id IS NOT NULL) + (stock_transfer_line_item_id IS NOT NULL) + (stock_adjustment_line_item_id IS NOT NULL) + (defective_stock_id IS NOT NULL) = 1`.
  - **Type ↔ source and type ↔ sign/status:** one CHECK per type, e.g. `transaction_type <> 'PURCHASE_RECEIPT' OR (goods_receipt_line_item_id IS NOT NULL AND quantity > 0 AND stock_status = 'SELLABLE')`. The full set follows the §5.0 table.
  - A reversal row has the opposite sign and the same source as its original (V-IN-06).
- **Indexes:** `(warehouse_id, product_id, transaction_at)` (stock card / reconciliation); `(transaction_type, transaction_at)`; `(product_id, transaction_at)`; `(inventory_batch_id)`; `(created_at)` (insert-only watermark). Each source FK is indexed automatically.
- **Behaviour:** Insert-only. Never updated or deleted (D33).
- **Populated:** Only by the movement service, always in the same transaction as (a) the source document change and (b) the `inventory` row update. It's never generated independently.

#### `inventory_batches`
A lot of a batch-tracked product, for traceability and FEFO (first-expiry-first-out). There's no balance column: quantity per batch is derived from the ledger (D1).

| Column | Type | NN | Notes |
|---|---|---|---|
| `inventory_batch_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `batch_number` | `VARCHAR(40)` | ✓ | UNIQUE. Internal lot number (`LOT-<grn>-<line>`) |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `goods_receipt_line_item_id` | `BIGINT UNSIGNED` | | FK, UNIQUE. Source: purchase |
| `stock_adjustment_line_item_id` | `BIGINT UNSIGNED` | | FK, UNIQUE. Source: opening stock / found stock |
| `supplier_batch_number` | `VARCHAR(40)` | | |
| `manufacture_date` | `DATE` | | |
| `expiry_date` | `DATE` | | Required when the product has expiry |
| `received_date` | `DATE` | ✓ | |
| `initial_quantity` | `DECIMAL(18,3)` | ✓ | Quantity when the lot was created (reference only) |

- **Constraints:** CHECK exactly one of the two source FKs is set; CHECK `initial_quantity > 0`; CHECK `expiry_date IS NULL OR manufacture_date IS NULL OR expiry_date > manufacture_date`.
- **Rules:** The product is batch-tracked and matches the source line's product. Ledger quantity per batch per warehouse never goes below 0 (V-IN-04).
- **Indexes:** `(product_id, expiry_date)` (FEFO picking, expiry write-offs).
- **Populated:** On receipt of a batch-tracked product, and in opening stock. A batch can move between warehouses through transfers. The ledger shows where its quantity is.

#### `inventory_reservations`
Sellable stock held for an order line at a warehouse.

| Column | Type | NN | Notes |
|---|---|---|---|
| `inventory_reservation_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `order_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `order_line_items` (1D) |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `reserved_quantity` | `DECIMAL(18,3)` | ✓ | |
| `fulfilled_quantity` | `DECIMAL(18,3)` | ✓ | Default 0. Shipped or handed over |
| `released_quantity` | `DECIMAL(18,3)` | ✓ | Default 0. Order cancelled or line reduced |
| `outstanding_quantity` | generated | | `reserved − fulfilled − released` STORED |
| `reserved_at` | `DATETIME(6)` | ✓ | |
| `closed_at` | `DATETIME(6)` | | When outstanding reached 0 |
| `reservation_status` | `VARCHAR(30)` | ✓ | `ACTIVE` / `PARTIALLY_FULFILLED` / `FULFILLED` / `RELEASED` |
| `release_reason` | `VARCHAR(100)` | | |

- **Keys:** UNIQUE `(order_line_item_id, warehouse_id)`. An order line split across warehouses has one reservation per warehouse.
- **Constraints:** CHECK `reserved_quantity > 0`; CHECK `fulfilled_quantity + released_quantity <= reserved_quantity`; CHECK `(reservation_status IN ('FULFILLED','RELEASED')) = (closed_at IS NOT NULL)`; CHECK `(released_quantity > 0) = (release_reason IS NOT NULL)`.
- **Rules:** `inventory.qty_reserved` = Σ outstanding of the warehouse × product's reservations (V-IN-03). Fulfilled qty = Σ issued qty for that order line at that warehouse.
- **Indexes:** `(warehouse_id, reservation_status)`.
- **Populated:** When an order is confirmed (1D). The allocator picks the nearest stocking warehouse (store first for STORE_PICKUP/COUNTER_SALE), splitting across warehouses when one can't cover the line. If total availability is short, the order waits as backordered (stock-shortage scenario) and reserves when stock arrives.

#### `stock_adjustments`

| Column | Type | NN | Notes |
|---|---|---|---|
| `stock_adjustment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `adjustment_number` | `VARCHAR(30)` | ✓ | UNIQUE |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `adjustment_date` | `DATE` | ✓ | |
| `adjustment_type` | `VARCHAR(30)` | ✓ | `OPENING_STOCK` / `CYCLE_COUNT` / `DAMAGE` / `SHRINKAGE` / `FOUND_STOCK` / `EXPIRY_WRITE_OFF` |
| `reason` | `VARCHAR(255)` | | |
| `adjustment_status` | `VARCHAR(30)` | ✓ | `DRAFT` / `PENDING_APPROVAL` / `POSTED` / `REJECTED` |
| `created_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`. Warehouse manager; Finance above a value threshold |
| `posted_at` | `DATETIME(6)` | | When ledger rows were written |

- **Constraints:** CHECK lists; CHECK `(adjustment_status = 'POSTED') = (posted_at IS NOT NULL)`; CHECK `adjustment_status <> 'POSTED' OR approved_by_employee_id IS NOT NULL OR adjustment_type = 'OPENING_STOCK'`.
- **Behaviour:** Ledger rows exist only for `POSTED` adjustments. A POSTED adjustment is never edited; a mistake is corrected with a new adjustment.
- **Populated:** Opening stock once per warehouse (D34). Monthly cycle counts on a rotating ABC sample (A items monthly, B quarterly, C half-yearly). Shrinkage ~0.2–0.5% of counted value (pack-dependent). Occasional found stock. Damage when warehouse handling breaks goods (S → D reclass). Expiry write-offs from a daily FEFO sweep of expired batches.

#### `stock_adjustment_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `stock_adjustment_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `stock_adjustment_id` | `BIGINT UNSIGNED` | ✓ | FK → `stock_adjustments` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `inventory_batch_id` | `BIGINT UNSIGNED` | | FK → `inventory_batches` (NULL for the opening-stock line that creates the batch) |
| `stock_status` | `VARCHAR(30)` | ✓ | Bucket adjusted |
| `system_quantity` | `DECIMAL(18,3)` | | Book quantity at count time (CYCLE_COUNT) |
| `counted_quantity` | `DECIMAL(18,3)` | | Physical count (CYCLE_COUNT) |
| `quantity_change` | `DECIMAL(18,3)` | ✓ | Signed, ≠ 0 |
| `unit_cost` | `DECIMAL(18,4)` | ✓ | MAC, or reference cost for opening/found stock (§4) |
| `value_change` | `DECIMAL(18,2)` | ✓ | Signed |
| `reason_code` | `VARCHAR(30)` | ✓ | `OPENING` / `COUNT_DIFFERENCE` / `DAMAGED` / `THEFT_OR_LOSS` / `EXPIRED` / `FOUND` / `RECLASS` |

- **Keys:** UNIQUE `(stock_adjustment_id, line_number)`.
- **Constraints:** CHECK `quantity_change <> 0`; CHECK `quantity_change * value_change >= 0`; CHECK `(system_quantity IS NULL) = (counted_quantity IS NULL)`; CHECK `counted_quantity IS NULL OR quantity_change = counted_quantity − system_quantity`; CHECK lists.
- **Rules:** A cycle-count line exists only where count ≠ book (a zero difference creates no line, but `inventory.last_counted_at` is still updated). A damage reclass is two lines (−SELLABLE, +DEFECTIVE) with net value 0, plus a `defective_stock` register entry (1D). Each posted line has exactly one ledger row (V-IN-05).

#### `stock_transfers`
Inter-warehouse transfer. For transfers between GSTINs it's also the tax invoice (D29).

| Column | Type | NN | Notes |
|---|---|---|---|
| `stock_transfer_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `transfer_number` | `VARCHAR(30)` | ✓ | UNIQUE (`STO/...`) |
| `transfer_type` | `VARCHAR(30)` | ✓ | `STORE_REPLENISHMENT` (DC → store) / `REBALANCING` (DC → DC) / `RETURN_TO_DC` (store → DC: defective or slow stock) |
| `source_warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `destination_warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `is_inter_gstin` | `BOOLEAN` | ✓ | Snapshot: source and destination GSTINs differ (⇒ inter-state ⇒ IGST) |
| `tax_invoice_number` | `VARCHAR(30)` | | UNIQUE (`STI/...`). Inter-GSTIN only |
| `eway_bill_number` | `CHAR(12)` | | Above the e-way bill threshold |
| `shipping_carrier_id` | `SMALLINT UNSIGNED` | | FK → `shipping_carriers`; NULL = own vehicle |
| `requested_date` | `DATE` | ✓ | |
| `dispatched_at` | `DATETIME(6)` | | |
| `expected_arrival_date` | `DATE` | | |
| `received_at` | `DATETIME(6)` | | |
| `taxable_amount` | `DECIMAL(18,2)` | ✓ | Default 0. Σ line transfer values |
| `igst_amount` | `DECIMAL(18,2)` | ✓ | Default 0 |
| `transfer_status` | `VARCHAR(30)` | ✓ | `DRAFT` / `APPROVED` / `IN_TRANSIT` / `RECEIVED` / `CANCELLED` |
| `created_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK |
| `received_by_employee_id` | `BIGINT UNSIGNED` | | FK |

- **Constraints:** CHECK `source_warehouse_id <> destination_warehouse_id`; CHECK lists; CHECK `is_inter_gstin OR (igst_amount = 0 AND tax_invoice_number IS NULL)`; CHECK `NOT is_inter_gstin OR transfer_status IN ('DRAFT','APPROVED','CANCELLED') OR tax_invoice_number IS NOT NULL` (issued by dispatch); CHECK `transfer_status NOT IN ('IN_TRANSIT','RECEIVED') OR dispatched_at IS NOT NULL`; CHECK `(transfer_status = 'RECEIVED') = (received_at IS NOT NULL)`; CHECK `received_at IS NULL OR received_at >= dispatched_at`.
- **Transitions:** DRAFT → APPROVED → IN_TRANSIT → RECEIVED; DRAFT/APPROVED → CANCELLED. No partial-receipt status: everything shipped arrives in one receipt, and quantities that didn't arrive in good condition are recorded per line as damaged or lost.
- **Rules:** `is_inter_gstin` matches the warehouses' registrations (V-IN-07). Store transfers stay within the same GSTIN by default (stores are in DC cities).
- **Populated:** Weekly store replenishment (store `qty_available` < reorder level → transfer from `replenishment_warehouse_id`, up to max level). Monthly DC rebalancing when one DC is below reorder level and another holds more than the max level for the same product (inter-state, so IGST). Store returns-to-DC of defective items. Transit time from city distance (warehouse coordinates). In-transit damage ~0.5% of lines, loss ~0.1%.

#### `stock_transfer_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `stock_transfer_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `stock_transfer_id` | `BIGINT UNSIGNED` | ✓ | FK → `stock_transfers` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `inventory_batch_id` | `BIGINT UNSIGNED` | | FK → `inventory_batches`. A line with several batches is split into several lines |
| `stock_status` | `VARCHAR(30)` | ✓ | Status at source: `SELLABLE`, or `DEFECTIVE` for RETURN_TO_DC |
| `quantity_requested` | `DECIMAL(18,3)` | ✓ | |
| `quantity_shipped` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `quantity_received_good` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `quantity_received_damaged` | `DECIMAL(18,3)` | ✓ | Default 0. Enters DEFECTIVE at the destination |
| `quantity_lost` | `DECIMAL(18,3)` | ✓ | Default 0. Never arrived; written off from in-transit (1E) |
| `unit_cost` | `DECIMAL(18,4)` | | Source MAC at dispatch |
| `transfer_value` | `DECIMAL(18,2)` | ✓ | Default 0. `ROUND(quantity_shipped × unit_cost, 2)` = GST taxable value |
| `gst_rate` | `DECIMAL(7,4)` | ✓ | Default 0. Product rate on the dispatch date (inter-GSTIN only) |
| `igst_amount` | `DECIMAL(18,2)` | ✓ | Default 0 |

- **Keys:** UNIQUE `(stock_transfer_id, line_number)`.
- **Constraints:** CHECK `quantity_requested > 0`; CHECK `quantity_shipped <= quantity_requested`; CHECK `quantity_received_good + quantity_received_damaged + quantity_lost <= quantity_shipped`; CHECK `quantity_shipped = 0 OR unit_cost IS NOT NULL`; CHECK all amounts `>= 0`.
- **Rules:** Once the header is RECEIVED, good + damaged + lost = shipped (V-IN-08). The `TRANSFER_OUT` ledger row = −shipped at the source; the `TRANSFER_IN` rows = +good (SELLABLE) and +damaged (DEFECTIVE) at the destination; lost has no ledger row, because the goods already left the source, and is written off in the GL (1E). Header amounts = Σ lines.

---

## 6. Relationship Summary

All FKs are `RESTRICT` (D15). "C" = composite FK. *(1D)* / *(1E)* / *(1G)* = target defined in that part.

| Child | Column(s) | Parent | Card. |
|---|---|---|---|
| `rfq_headers` | `delivery_warehouse_id` / `business_unit_id` / `created_by_employee_id` | `warehouses` / `business_units` / `employees` | N:1 |
| `rfq_line_items` | `rfq_id` / `product_id` | `rfq_headers` / `products` | N:1 |
| `rfq_suppliers` | `rfq_id` / `supplier_id` | `rfq_headers` / `suppliers` | M:N bridge |
| `rfq_status_history` | `rfq_id` | `rfq_headers` | N:1 |
| `quotation_headers` | `rfq_supplier_id` | `rfq_suppliers` | 1:0..1 |
| `quotation_headers` | `payment_term_id` / `evaluated_by_employee_id` | `payment_terms` / `employees` | N:1 |
| `quotation_line_items` | `quotation_id` / `rfq_line_item_id` | `quotation_headers` / `rfq_line_items` | N:1 |
| `quotation_status_history` | `quotation_id` | `quotation_headers` | N:1 |
| `purchase_order_headers` | `quotation_id` (UNIQUE) | `quotation_headers` | 0..1:1 (an awarded quotation yields exactly one PO) |
| `purchase_order_headers` | `supplier_id` / `supplier_address_id` / `delivery_warehouse_id` / `payment_term_id` | suppliers / addresses / warehouses / terms | N:1 |
| `purchase_order_line_items` | `purchase_order_id` / `product_id` | headers / `products` | N:1 |
| `purchase_order_line_items` | `quotation_line_item_id` | `quotation_line_items` | 0..1:1 |
| `purchase_order_status_history` | `purchase_order_id` | headers | N:1 |
| `goods_receipts` | C `(purchase_order_id, warehouse_id)` | `purchase_order_headers` | N:1 |
| `goods_receipt_line_items` | `goods_receipt_id` / `purchase_order_line_item_id` / `receiving_location_id` | GRN / PO line / location | N:1 |
| `goods_inspections` | `goods_receipt_line_item_id` | GRN line | 1:1 |
| `supplier_invoices` | `supplier_id` / `purchase_order_id` / `company_gst_registration_id` | suppliers / PO / GSTIN | N:1 |
| `supplier_invoice_line_items` | `supplier_invoice_id` / `purchase_order_line_item_id` | invoice / PO line | N:1 |
| `supplier_payments` | `supplier_id` / `bank_account_id` / `payment_method_id` | suppliers / bank / method | N:1 |
| `inventory` | `warehouse_id` / `product_id` | warehouses / products | N:1 (UNIQUE pair) |
| `inventory_transactions` | `warehouse_id`, `product_id`, `inventory_batch_id`, `warehouse_location_id`, `generation_run_id` *(1G)* | … | N:1 |
| `inventory_transactions` | 8 source FKs (§5.0), exactly one set | GRN line, shipment line *(1D)*, invoice line *(1D)*, return line *(1D)*, return inspection *(1D)*, transfer line, adjustment line, defective stock *(1D)* | N:1 |
| `inventory_transactions` | `reversal_of_inventory_transaction_id` | self | 0..1:1 |
| `inventory_batches` | `product_id` / `goods_receipt_line_item_id` / `stock_adjustment_line_item_id` | products / GRN line / adj. line | N:1 / 1:1 / 1:1 |
| `inventory_reservations` | `order_line_item_id` *(1D)* / `warehouse_id` | order line / warehouses | N:1 |
| `stock_adjustments` | `warehouse_id` / employees | | N:1 |
| `stock_adjustment_line_items` | `stock_adjustment_id` / `product_id` / `inventory_batch_id` | | N:1 |
| `stock_transfers` | `source_warehouse_id` / `destination_warehouse_id` / `shipping_carrier_id` / employees | | N:1 |
| `stock_transfer_line_items` | `stock_transfer_id` / `product_id` / `inventory_batch_id` | | N:1 |

**Traceability check (master prompt §15A):** RFQ → `rfq_suppliers` → `quotation_headers` → `quotation_line_items` → `purchase_order_line_items` → `goods_receipt_line_items` → `goods_inspections` → `inventory_transactions` (FK GRN line) and `inventory_batches` → `supplier_invoice_line_items` (FK PO line) → AP / allocations / payments / journals (1E). It can be traversed both ways with plain joins.

---

## 7. Population Order and Change Behaviour

**Insert order within a business event** (always one DB transaction per event): header → lines → status history row → inventory batch (if any) → `inventory_transactions` → `inventory` update → PO-line / reservation counters → (1E) journal entry.

**Change behaviour (for Q5 in 1G):**

| Behaviour | Tables |
|---|---|
| Insert-only | `inventory_transactions`, `inventory_batches`, all `*_status_history`, `goods_inspections`, `rfq_line_items`, `quotation_line_items`, `goods_receipt_line_items`, `supplier_invoice_line_items`, `stock_adjustment_line_items` |
| Insert + status/counter updates | `rfq_headers`, `rfq_suppliers`, `quotation_headers`, `purchase_order_headers`, `purchase_order_line_items`, `goods_receipts`, `supplier_invoices`, `supplier_payments`, `inventory_reservations`, `stock_adjustments`, `stock_transfers`, `stock_transfer_line_items` |
| Mutable snapshot | `inventory` |

**Indicative volumes (Medium, 2 years):**

| Table | Rows |
|---|---|
| RFQs / lines / invited suppliers | ~2,000 / ~8,000 / ~7,000 |
| Quotations / lines | ~4,900 / ~17,000 |
| POs / lines | ~5,000 / ~20,000 |
| GRNs / lines / inspections | ~6,500 / ~24,000 / ~24,000 |
| Supplier invoices / lines | ~6,300 / ~23,000 |
| Supplier payments | ~3,500 |
| `inventory` | ~10,000 |
| `inventory_transactions` | ~1.5–2 million (dominated by sales issues) |
| Stock adjustments / lines | ~250 / ~6,000 |
| Stock transfers / lines | ~1,200 / ~15,000 |

---

## 8. Validation Rules (procurement and inventory)

The constraints above already catch most errors. These cross-table rules become Phase 3 tests and Validation-screen checks. The master prompt's reconciliations (§10, §15E) are marked ★.

| ID | Rule |
|---|---|
| V-PR-01 | Every header's tax and total columns equal the sum of its lines plus round-off; line tax amounts follow §2.3 for the header's `is_inter_state` and the GST rate on the document date |
| V-PR-02 | Status-history chains are continuous and end at the header's status; only allowed transitions occur |
| V-PR-03 | RFQ lines belong to the RFQ's business unit; invited suppliers are active approved suppliers (`supplier_products`) for at least one line |
| V-PR-04 | Every QUOTED invitation has exactly one quotation; DECLINED/NO_RESPONSE have none |
| V-PR-05 | Quotation status agrees with its awarded lines; the RFQ status agrees with its lines' award status |
| V-PR-06 | Quotation lines reference RFQ lines of the same RFQ; quoted qty ≤ requested; quotation date within the response window |
| V-PR-07 | RFQ_AWARD POs: supplier = quotation supplier, PO lines map 1:1 to awarded quotation lines with the same product, price and rate; PO date after the award |
| ★ V-PR-08 | PO line counters = Σ GRN lines (received), Σ inspections (accepted / rejected), Σ supplier invoice lines (invoiced) |
| V-PR-09 | GRN date ≥ PO date and on or after the PO reached SENT; only APPROVED-and-later POs receive goods; PO status matches its line quantities |
| V-PR-10 | GRN lines reference lines of the GRN's PO; locations are in the GRN warehouse |
| ★ V-PR-11 | Inspection accepted + rejected = GRN line received; ledger PURCHASE_RECEIPT qty for the GRN line = accepted qty; rejected qty has no ledger row |
| ★ V-PR-12 | Supplier invoice: PO supplier = invoice supplier; bill-to GSTIN = PO warehouse GSTIN; cumulative invoiced ≤ accepted per PO line; supplier invoice totals = AP open item (1E) |
| V-PR-13 | Supplier invoice lines reference lines of the invoice's PO; `po_unit_price` = PO line price |
| V-PR-14 | Approvers were active and within `approval_limit` on the approval date (POs, invoices, payments, adjustments) |
| ★ V-IN-01 | For every warehouse × product × status: Σ ledger quantity = the `inventory` column; Σ ledger value_change = `stock_value` |
| V-IN-02 | Replaying the ledger in time order never takes any warehouse × product × status below 0 (never negative at any moment, not just at the end) |
| ★ V-IN-03 | `qty_reserved` = Σ outstanding reservations; reservation fulfilled qty = Σ issue quantities for that order line and warehouse |
| V-IN-04 | Batch quantity per warehouse (Σ ledger by batch) is never negative; batch-tracked products always carry a batch on the ledger; no issue of an expired batch to a customer |
| V-IN-05 | Every POSTED adjustment line, inspected GRN line, dispatched/received transfer line etc. has exactly the expected ledger rows (§5.0), and no ledger row lacks its source event |
| V-IN-06 | Reversal rows mirror their original (same source, opposite quantity and value) and each original is reversed at most once |
| V-IN-07 | Transfers: `is_inter_gstin` matches the warehouses' GSTINs; inter-GSTIN lines carry IGST at the product rate on the dispatch date; same-GSTIN transfers carry none |
| ★ V-IN-08 | Received transfers: good + damaged + lost = shipped; TRANSFER_OUT and TRANSFER_IN ledger quantities match the line |
| V-IN-09 | Issue unit cost = the warehouse MAC at that moment (recomputed by replaying the ledger) within ±0.0001; return receipt cost = original issue cost |
| V-IN-10 | Ledger `transaction_at` is not earlier than its source document's business date and is within the generation run's simulated window |

---

## 9. What Comes Next

Part 1D: sales, shipping, invoicing and returns tables (`orders`, shipments, invoices, credit notes, returns, refunds, `defective_stock`), including the forward-referenced FKs above and Q6 (product price history).
