# Phase 1 – Part 1D: Sales, Shipping, Invoicing and Returns Tables

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9). Baseline for Phase 2
**Last updated:** 2026-11-08 (amended by Part 1E: D60, D63; Part 1H: D79 tax-inclusive B2C pricing, D80 composite FKs, F5)
**Builds on:** Part 1A (D1–D14, A1 attribution / stores), Part 1B (master data, D15–D24), Part 1C (procurement / inventory, D25–D36, document conventions §2)

---

## 0. Scope

This part covers **18 tables**: columns, keys, constraints, relationships and population for the order-to-cash and returns modules. It also defines the targets of the FKs that Part 1C referenced forward (`order_line_items`, `shipment_line_items`, `invoice_line_items`, `sales_return_line_items`, `return_inspections`, `defective_stock`) and resolves Q6.

| Group | Tables | Count |
|---|---|---|
| Sales & orders | `orders`, `order_line_items`, `order_status_history`, `customer_payments` | 4 |
| Shipping & logistics | `shipments`, `shipment_line_items`, `shipment_status_history`, `shipment_tracking`, `delivery_attempts` | 5 |
| Invoicing | `invoices`, `invoice_line_items`, `credit_notes`, `credit_note_line_items` | 4 |
| Returns, refunds & defective stock | `sales_returns`, `sales_return_line_items`, `return_inspections`, `refunds`, `defective_stock` | 5 |

**The table count is unchanged (92).** `customer_payment_allocations` and `accounts_receivable` belong to Part 1E. Conventions from 1B §2 (types, audit columns, FK policy) and 1C §2 (document numbers, status history, amount pattern) apply throughout.

---

## 1. Decisions Made in This Part

### 1.1 New decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| D37 | Product price history (Q6) | **No history table.** `products.list_price` is updated in place. Every order line snapshots `list_price` and the actual `unit_price` | The OLTP system only needs today's price. Real ERPs often keep no price history, and analysts rebuild it with **dbt snapshots / CDC** on `products`. That's a core learning exercise this project should leave to the analytics side. The order-line snapshot already answers "what did this customer pay". It also avoids a table the master prompt would call unnecessary |
| D38 | Tax document numbers (**amends 1C §2.1**) | GST tax documents use a compact number of **at most 16 characters**, unique per GSTIN per FY: `<STATE><FY4>-<T><SEQ6>`, e.g. `KA2627-I000123`. `T` = `I` tax invoice, `C` credit note, `T` stock-transfer tax invoice. Non-tax documents keep the readable `<PREFIX>/<STATE>/<FY>/<SEQ6>` format | GST law caps invoice serial numbers at 16 characters. `INV/KA/2026-27/000123` would be illegal, so the 1C `STI/...` format is corrected |
| D39 | Orders are not tax documents | Order lines carry the **total** GST (rate × taxable). The CGST/SGST vs IGST split is decided per **invoice**, because it depends on the dispatching warehouse's state, which is only known at allocation (an order can ship from two states) | Avoids recalculating order tax when allocation changes. Matches real practice: a sales order is a commercial document, the invoice is the tax document |
| D40 | Invoice cancellation | An invoice may be **cancelled** only within 24 hours of issue and before the shipment is `IN_TRANSIT` (e.g. the carrier didn't collect and the order was cancelled at the dock). The stock issue is reversed (D33). After that, corrections are **credit notes only** | Mirrors GST/e-invoice rules (an IRN can only be cancelled within 24 h) and gives the master prompt's "invoice cancellation" a lawful, realistic shape |
| D41 | Return to origin (RTO) | A shipment that fails delivery after the final attempt comes back to the warehouse: a new movement type `RTO_RECEIPT` (+SELLABLE, at the original issue cost, source = shipment line) and a credit note with reason `RTO` for the shipment's invoice. **Amends the 1C movement map** | Invoices are raised at dispatch (D4), so undelivered goods must be credited and restocked. RTO is very common in Indian e-commerce (especially COD) |
| D42 | Shipping charges | The order carries `shipping_charge_amount`. It's billed once, on the order's first invoice, as an invoice line of type `SHIPPING_CHARGE`. It's taxed at the highest GST rate among that invoice's goods (a simplified composite-supply rule) and posted to Shipping Income (1B role `SHIPPING_INCOME`) | Real B2C behaviour (free shipping above a threshold) with a clear tax rule and no extra table |
| D43 | Cash on delivery (COD) | The carrier collects cash at the door. That creates a `customer_payments` row (method `CASH`, `collected_by_shipping_carrier_id` set) dated on delivery, with `settlement_date` = the carrier's remittance date (delivery + 7 days by default). Until then it sits in Payment Clearing | Realistic COD cash flow, gives bank reconciliation real timing differences, and needs no extra table |
| D44 | Credit timing for returns | A return request can be rejected before pickup (outside the return window, non-returnable product); no goods move. For approved returns, the **credit note is issued after the final inspection** and covers all received quantity, sellable or defective. Refunds pay out credit notes (R5) | Matches e-commerce practice: "refund after QC". Defective goods are still the company's liability when the reason is valid |
| D45 | Defective-stock register | `defective_stock` = one row per defect event, linked to its cause with the "exactly one source FK" pattern (D28): return inspection, stock adjustment (warehouse damage) or transfer line (damaged in transit). Disposition always covers the whole row quantity | Satisfies the master prompt: every defective quantity has a reason, an inspection result and a movement history, with real FKs |
| D46 | e-Invoicing | Invoices and credit notes to GST-registered customers carry an `irn` (Invoice Reference Number, a 64-character hash) and `irn_generated_at`; B2C documents don't | The fictional company is above the e-invoicing turnover threshold. Cheap to model, and realistic for B2B analytics |
| D47 | Credit hold | A B2B order that would exceed the customer's credit limit, or whose customer has overdue invoices beyond a grace period, enters `ON_HOLD`. It's released when payments arrive (or after a Finance override), otherwise cancelled after 14 days | A classic order-to-cash control. It links AR ageing (1E) to order flow, and gives order lifecycles a realistic "stuck" state |
| D48 | Line correspondence | Shipment lines ↔ order lines (N:1); invoice lines ↔ shipment lines (**1:1**) for shipped orders, or ↔ order lines for handover sales; return lines ↔ invoice lines (N:1); credit-note lines ↔ invoice lines (N:1) | Each quantity can be traced exactly from order → shipment → invoice → return → credit note → refund, in both directions (master prompt §15A) |

### 1.2 Amendments to Part 1C (made in the same commit)

| 1C location | Change |
|---|---|
| §2.1 document numbers | Tax documents (`STI` transfer invoice) use the D38 compact format; `tax_invoice_number` becomes `VARCHAR(16)` |
| §5.0 movement map | New row `RTO_RECEIPT` (+, SELLABLE, source `shipment_line_item_id`) (D41) |
| §7 volumes | `inventory_transactions` corrected from "1.5–2 million" to **~250,000** for the Medium preset. The earlier figure was wrong: at 50,000 orders (~140,000 issue rows) plus receipts, transfers and adjustments, the ledger is ~250k rows. Large preset ≈ 1.2M |

### 1.3 Open questions

None new. Q6 is resolved by D37. Q3 (AP open items) remains for 1E.

---

## 2. Order-to-Cash Flow

```
order placed (channel, attribution, prices snapshotted)
  ├─ B2C prepaid ─► payment attempt(s) ─ success ─► CONFIRMED          (failure ×3 → CANCELLED)
  ├─ B2C COD / B2B credit ─► credit check ─ ok ─► CONFIRMED            (B2B over limit → ON_HOLD, D47)
  └─ COUNTER_SALE ─► paid at counter ─► invoice + issue at once (no shipment)
CONFIRMED ─► reserve stock per warehouse (1C)  ─ short? ─► BACKORDERED until stock arrives
          ─► per warehouse: shipment PICKING → PACKED → PICKED_UP
                 at PICKED_UP (dispatch): SALES_ISSUE (COGS), tax invoice (D4), reservation fulfilled
          ─► carrier: IN_TRANSIT → OUT_FOR_DELIVERY → delivery attempt(s)
                 ├─ DELIVERED  (COD cash collected, D43)
                 └─ 3 failures ─► RETURNED_TO_SENDER ─► RTO_RECEIPT + credit note (D41)
invoice ─► AR open item (1E) ─► payments (partial / multiple / failed) ─► allocations (1E)
delivered line ─► return request ─► approved ─► goods received (QUARANTINE)
          ─► inspection ─► SELLABLE / DEFECTIVE (+ defective_stock) / further inspection
          ─► credit note ─► refund (pending approval → completed) if the customer had paid
```

`STORE_PICKUP`: the order reserves at the chosen store (stock transferred from the DC if needed). Invoice and issue happen at handover; there's no shipment.

---

## 3. Sales & Order Tables

#### `orders`

| Column | Type | NN | Notes |
|---|---|---|---|
| `order_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `order_number` | `VARCHAR(30)` | ✓ | UNIQUE. `SO/<HQ state>/<FY>/<SEQ>`. Commercial document, HQ series |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK → `customers` |
| `order_placed_at` | `DATETIME(6)` | ✓ | Business timestamp |
| `order_date` | `DATE` | ✓ | `= DATE(order_placed_at)` (CHECK) |
| `sales_channel_id` | `SMALLINT UNSIGNED` | ✓ | FK → `sales_channels` (A1) |
| `marketing_channel_id` | `SMALLINT UNSIGNED` | ✓ | FK → `marketing_channels` (last touch) |
| `marketing_campaign_id` | `BIGINT UNSIGNED` | | FK → `marketing_campaigns` |
| `fulfilment_type` | `VARCHAR(20)` | ✓ | `SHIP` / `STORE_PICKUP` / `COUNTER_SALE` |
| `store_warehouse_id` | `SMALLINT UNSIGNED` | | FK → `warehouses`. Store for pickup / counter sale |
| `billing_address_id` | `BIGINT UNSIGNED` | ✓ | FK → `customer_addresses` (live default billing at order time) |
| `shipping_address_id` | `BIGINT UNSIGNED` | | FK → `customer_addresses`. Required for SHIP |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_terms` (snapshot from the customer, or `ADVANCE`/`COD` chosen at checkout) |
| `sales_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`. Field rep or store staff |
| `customer_po_reference` | `VARCHAR(40)` | | B2B customer's own PO number |
| `requested_delivery_date` | `DATE` | | |
| `items_taxable_amount` | `DECIMAL(18,2)` | ✓ | Σ line taxable |
| `items_tax_amount` | `DECIMAL(18,2)` | ✓ | Σ line tax (total GST, D39) |
| `shipping_charge_amount` | `DECIMAL(18,2)` | ✓ | Default 0. Excluding GST (D42) |
| `prices_include_tax` | `BOOLEAN` | ✓ | TRUE for B2C (shelf / MRP prices include GST), FALSE for B2B (D79, Part 1H F2) |
| `order_total_estimate` | `DECIMAL(18,2)` | ✓ | Items + tax + shipping (+ its GST). An estimate: invoices are authoritative |
| `order_status` | `VARCHAR(30)` | ✓ | See transitions |
| `hold_reason` | `VARCHAR(30)` | | `CREDIT_LIMIT` / `OVERDUE_BALANCE` |
| `cancelled_at` | `DATETIME(6)` | | |
| `cancellation_reason` | `VARCHAR(30)` | | `PAYMENT_FAILED` / `CUSTOMER_REQUEST` / `OUT_OF_STOCK` / `CREDIT_HOLD_EXPIRED` / `DUPLICATE_ORDER` |

- **Statuses and transitions:** `PENDING_PAYMENT` (prepaid, awaiting payment) → `CONFIRMED`; `PENDING_CREDIT_CHECK` → `CONFIRMED` / `ON_HOLD`; `ON_HOLD` → `CONFIRMED` / `CANCELLED`; `CONFIRMED` ↔ `BACKORDERED`; `CONFIRMED` → `PARTIALLY_SHIPPED` → `SHIPPED` → `PARTIALLY_DELIVERED` → `DELIVERED` → `CLOSED` (return window over and fully invoiced); pre-dispatch states → `CANCELLED`. An order with some lines cancelled continues with the rest; the cancelled lines carry `quantity_cancelled`.
- **Constraints:** CHECK lists; CHECK `(fulfilment_type = 'SHIP') = (shipping_address_id IS NOT NULL)`; CHECK `(fulfilment_type = 'SHIP') = (store_warehouse_id IS NULL)`; CHECK `fulfilment_type <> 'COUNTER_SALE' OR shipping_charge_amount = 0`; CHECK `(order_status = 'ON_HOLD') = (hold_reason IS NOT NULL)`; CHECK `(order_status = 'CANCELLED') = (cancelled_at IS NOT NULL)`; CHECK `cancelled_at IS NULL OR cancellation_reason IS NOT NULL`; CHECK `order_date = DATE(order_placed_at)`; amounts `>= 0`.
- **Rules (validation):** Addresses belong to the customer and were live at `order_placed_at`. Sales channel allowed for the customer type. Campaign on the same marketing channel, with `order_date` inside its window (A1). `COUNTER_SALE` only through `RETAIL_STORE`. The store is a `RETAIL_STORE` warehouse. Header amounts = Σ lines (V-SO-01).
- **Keys:** UNIQUE `(order_id, customer_id)`, the target of the composite FKs from `invoices` and `customer_payments` (D80).
- **Indexes:** `(customer_id, order_date)`, `(order_status, order_date)`, `(order_date)`, `(sales_channel_id, order_date)`, `(marketing_campaign_id)`.
- **Populated:** By the daily demand engine. Orders/day = base × monthly seasonality (pack) × weekday factor × trend × campaign uplift. B2B/B2C share by **value** (D23). Customers are chosen with a Pareto skew (a minority of B2B accounts drives most revenue) and B2C repeat-purchase probability. Products are chosen by popularity (Zipf within BU, only launched and not discontinued). Basket size and quantities by segment. Channel and attribution from the pack's mix for the customer type (A1 rules). Defaults: ~4% of orders cancelled, ~3% backordered, ~5% of B2B orders pass through `ON_HOLD`.

#### `order_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `order_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `order_id` | `BIGINT UNSIGNED` | ✓ | FK → `orders` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `quantity_ordered` | `DECIMAL(18,3)` | ✓ | |
| `list_price` | `DECIMAL(18,4)` | ✓ | Snapshot of `products.list_price` at order time (D37) |
| `unit_price` | `DECIMAL(18,4)` | ✓ | Price charged before discount. Usually = list price; B2B negotiated prices differ |
| `discount_percent` | `DECIMAL(7,4)` | ✓ | Default 0 |
| `discount_type` | `VARCHAR(30)` | | `VOLUME` / `SEGMENT` / `CAMPAIGN_PROMO` / `CLEARANCE` |
| `inclusive_unit_price` | `DECIMAL(18,4)` | | Price per unit **including GST**, fixed at order time; set iff the order `prices_include_tax`. Then `taxable_amount = ROUND(qty × inclusive × (1 − disc/100) / (1 + rate/100), 2)` and `unit_price` = `inclusive / (1 + rate/100)` (D79) |
| `taxable_amount` | `DECIMAL(18,2)` | ✓ | `ROUND(qty × unit_price × (1 − disc/100), 2)` |
| `gst_rate` | `DECIMAL(7,4)` | ✓ | Product rate on `order_date` |
| `tax_amount` | `DECIMAL(18,2)` | ✓ | `ROUND(taxable × rate/100, 2)`. Total GST, not split (D39) |
| `line_total` | `DECIMAL(18,2)` | ✓ | `taxable + tax` |
| `quantity_shipped` | `DECIMAL(18,3)` | ✓ | Default 0. Dispatched or handed over |
| `quantity_delivered` | `DECIMAL(18,3)` | ✓ | Default 0. Delivered or handed over |
| `quantity_invoiced` | `DECIMAL(18,3)` | ✓ | Default 0. Net of cancelled invoices |
| `quantity_cancelled` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `quantity_rto` | `DECIMAL(18,3)` | ✓ | Default 0. Shipped but returned to origin (D41) |
| `quantity_return_requested` | `DECIMAL(18,3)` | ✓ | Default 0. Σ approved return quantity |
| `quantity_returnable` | generated | | `quantity_delivered − quantity_return_requested` STORED |
| `line_status` | `VARCHAR(30)` | ✓ | `OPEN` / `BACKORDERED` / `PARTIALLY_SHIPPED` / `SHIPPED` / `DELIVERED` / `CANCELLED` / `CLOSED` |

- **Keys:** UNIQUE `(order_id, line_number)`; UNIQUE `(order_id, product_id)`. UNIQUE `(order_line_item_id, product_id)`, the target of the composite FK from `invoice_line_items` (D80).
- **Constraints:** CHECK `quantity_ordered > 0`; CHECK `unit_price > 0`; CHECK `discount_percent BETWEEN 0 AND 90`; CHECK `(discount_percent > 0) = (discount_type IS NOT NULL)`; CHECK `line_total = taxable_amount + tax_amount`; CHECK `quantity_shipped + quantity_cancelled <= quantity_ordered`; CHECK `quantity_delivered + quantity_rto <= quantity_shipped`; CHECK `quantity_invoiced <= quantity_shipped` (invoicing happens at dispatch / handover); CHECK `quantity_return_requested <= quantity_delivered` (**returns never exceed delivered**, enforced by the database); all quantities `>= 0`.
- **Rules:** Counters reconcile to shipments, invoices, RTOs and returns (V-SO-03, D32). Selling price incl. GST ≤ MRP (V-MD-18 applied to the line).
- **Indexes:** `(product_id)`, `(line_status)`.

#### `order_status_history`
Standard structure (1C §2.2), FK `order_id`.

#### `customer_payments`
Every payment attempt and receipt from a customer (D5). Allocation to invoices is in `customer_payment_allocations` (1E).

| Column | Type | NN | Notes |
|---|---|---|---|
| `customer_payment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `payment_number` | `VARCHAR(30)` | ✓ | UNIQUE (`RCPT/<state>/<FY>/<SEQ>`) |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK → `customers` |
| `order_id` | `BIGINT UNSIGNED` | | FK → `orders`. Set for order prepayments, counter sales and COD; NULL for B2B payments against invoices |
| `payment_type` | `VARCHAR(30)` | ✓ | `ORDER_PREPAYMENT` / `COUNTER_PAYMENT` / `COD_COLLECTION` / `INVOICE_PAYMENT` / `ADVANCE` |
| `attempt_number` | `TINYINT UNSIGNED` | ✓ | Default 1. Retry sequence for the same order |
| `initiated_at` | `DATETIME(6)` | ✓ | |
| `payment_date` | `DATE` | ✓ | Business date of receipt |
| `payment_method_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_methods` (`usable_for_receipts`) |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK → `bank_accounts` (COLLECTIONS, or petty cash for store counter cash) |
| `amount` | `DECIMAL(18,2)` | ✓ | |
| `gateway_fee_amount` | `DECIMAL(18,2)` | ✓ | Default 0. `amount × method fee%` (→ payment gateway charges) |
| `collected_by_shipping_carrier_id` | `SMALLINT UNSIGNED` | | FK → `shipping_carriers`. COD (D43) |
| `gateway_reference` | `VARCHAR(40)` | | Gateway txn id / UTR / cheque number |
| `payment_status` | `VARCHAR(30)` | ✓ | `INITIATED` / `SUCCESS` / `FAILED` / `REVERSED` |
| `failure_reason` | `VARCHAR(40)` | | `INSUFFICIENT_FUNDS` / `BANK_DECLINED` / `TIMEOUT` / `AUTH_FAILED` / `CHEQUE_BOUNCED` |
| `completed_at` | `DATETIME(6)` | | When SUCCESS/FAILED was decided |
| `settlement_date` | `DATE` | | Date the money reaches the bank (method lag, or carrier remittance for COD) |
| `settlement_bank_transaction_id` | `BIGINT UNSIGNED` | | FK → `bank_transactions`. The settlement / remittance line that paid out this clearing-method receipt (D63, Part 1E) |
| `reversed_at` | `DATETIME(6)` | | Chargeback / bounce after success |
| `reversal_reason` | `VARCHAR(40)` | | |

- **Keys:** UNIQUE `(order_id, attempt_number)` (multiple NULL `order_id`s allowed). Composite FK `(order_id, customer_id)` → `orders (order_id, customer_id)`; not checked when `order_id` is NULL (D80).
- **Constraints:** CHECK `amount > 0`; CHECK `gateway_fee_amount >= 0 AND gateway_fee_amount < amount`; CHECK lists; CHECK `(payment_status = 'FAILED') = (failure_reason IS NOT NULL)`; CHECK `payment_status = 'INITIATED' OR completed_at IS NOT NULL`; CHECK `(payment_status = 'REVERSED') = (reversed_at IS NOT NULL AND reversal_reason IS NOT NULL)`; CHECK `(payment_type = 'COD_COLLECTION') = (collected_by_shipping_carrier_id IS NOT NULL)`; CHECK `payment_status <> 'SUCCESS' OR settlement_date IS NOT NULL`; CHECK `payment_type IN ('INVOICE_PAYMENT','ADVANCE') OR order_id IS NOT NULL`.
- **Rules:** Only `SUCCESS` payments are allocated (D5). Amount within method limits (e.g. cash < ₹2,00,000). `settlement_date ≥ payment_date`. A `REVERSED` payment's allocations are reversed (1E).
- **Indexes:** `(customer_id, payment_date)`, `(payment_status, payment_date)`, `(settlement_date)`, `(order_id)`.
- **Populated:**
  - **B2C prepaid:** 1–3 attempts at checkout, ~8% failure per attempt; if all fail, the order is cancelled `PAYMENT_FAILED`.
  - **COD** (~30% of B2C SHIP orders by default): created on successful delivery.
  - **Counter sales:** cash/UPI/card at handover.
  - **B2B:** by payment behaviour profile per customer (on time / slightly late / chronic late), often paying several invoices at once, sometimes partially. Cheques bounce ~2%.
  - Rare card chargebacks become `REVERSED`.

---

## 4. Shipping & Logistics Tables

#### `shipments`
A dispatch from one warehouse for one order. Split shipments = several rows.

| Column | Type | NN | Notes |
|---|---|---|---|
| `shipment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `shipment_number` | `VARCHAR(30)` | ✓ | UNIQUE (`SHP/<state>/<FY>/<SEQ>`) |
| `order_id` | `BIGINT UNSIGNED` | ✓ | FK → `orders` |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses`. Dispatching warehouse |
| `shipping_address_id` | `BIGINT UNSIGNED` | ✓ | FK → `customer_addresses` (= order shipping address) |
| `shipping_carrier_id` | `SMALLINT UNSIGNED` | ✓ | FK → `shipping_carriers` |
| `tracking_number` | `VARCHAR(30)` | ✓ | UNIQUE. AWB number with the carrier's prefix |
| `is_cod` | `BOOLEAN` | ✓ | |
| `cod_amount` | `DECIMAL(18,2)` | | Amount to collect = this shipment's invoice total |
| `package_count` | `SMALLINT UNSIGNED` | ✓ | |
| `total_weight_kg` | `DECIMAL(10,3)` | ✓ | Σ line quantity × product weight |
| `freight_cost_amount` | `DECIMAL(18,2)` | ✓ | Carrier rate-card cost (excl. GST). Reconciles to the carrier's monthly freight bill (1E) |
| `eway_bill_number` | `CHAR(12)` | | Above ₹50,000 consignment value |
| `planned_dispatch_date` | `DATE` | ✓ | |
| `dispatched_at` | `DATETIME(6)` | | Handed to carrier (PICKED_UP) = invoice + stock issue time |
| `expected_delivery_date` | `DATE` | | |
| `delivered_at` | `DATETIME(6)` | | |
| `rto_received_at` | `DATETIME(6)` | | Back at the warehouse (D41) |
| `shipment_status` | `VARCHAR(30)` | ✓ | See transitions |

- **Statuses (R11; master prompt list, shipment-level):** `PENDING` → `PICKING` → `PICKED` → `PACKING` → `PACKED` → `READY_FOR_PICKUP` → `PICKED_UP` → `IN_TRANSIT` → `OUT_FOR_DELIVERY` → `DELIVERED`. `OUT_FOR_DELIVERY` → `DELIVERY_FAILED` → `OUT_FOR_DELIVERY` (re-attempt) or → `RETURNED_TO_SENDER`. Any state before `IN_TRANSIT` → `CANCELLED` (after `PICKED_UP` only under D40). `Order Confirmed` is an **order** status (R11).
- **Constraints:** CHECK lists; CHECK `is_cod = (cod_amount IS NOT NULL)`; CHECK `cod_amount IS NULL OR cod_amount > 0`; CHECK `shipment_status IN ('PENDING','PICKING','PICKED','PACKING','PACKED','READY_FOR_PICKUP','CANCELLED') OR dispatched_at IS NOT NULL`; CHECK `(shipment_status = 'DELIVERED') = (delivered_at IS NOT NULL)`; CHECK `(shipment_status = 'RETURNED_TO_SENDER') = (rto_received_at IS NOT NULL)`; CHECK `delivered_at IS NULL OR delivered_at > dispatched_at`; CHECK `package_count > 0 AND total_weight_kg > 0`.
- **Rules:** The order is `SHIP`. The warehouse has reservations for the order. The carrier supports COD when `is_cod`. `dispatched_at ≥ order_placed_at` (V-SH-01).
- **Indexes:** `(order_id)`, `(shipment_status, planned_dispatch_date)`, `(shipping_carrier_id, dispatched_at)`, `(dispatched_at)`.
- **Populated:** When reservations for an order at a warehouse are complete (or the partial-shipment policy allows), one shipment per warehouse per wave. Same-day or next-day dispatch at DCs. Carrier chosen by service level, distance and COD need. Transit days from the distance between the warehouse and the delivery state's region plus carrier performance. Defaults: ~10% of orders split, ~8% of shipments with a failed attempt, ~3% RTO (higher for COD).

#### `shipment_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `shipment_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `shipment_id` | `BIGINT UNSIGNED` | ✓ | FK → `shipments` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `order_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `order_line_items` |
| `inventory_batch_id` | `BIGINT UNSIGNED` | | FK → `inventory_batches`. One line per batch (FEFO) |
| `quantity_shipped` | `DECIMAL(18,3)` | ✓ | |
| `picked_from_location_id` | `BIGINT UNSIGNED` | | FK → `warehouse_locations` |

- **Keys:** UNIQUE `(shipment_id, line_number)`; UNIQUE `(shipment_id, order_line_item_id, inventory_batch_id)`. Batch can be NULL; for non-batch products the generator emits one line per order line.
- **Constraints:** CHECK `quantity_shipped > 0`.
- **Rules:** The order line belongs to the shipment's order. Batch and location belong to the product and warehouse. The ledger `SALES_ISSUE` for the line = −quantity_shipped at dispatch (V-SH-02).
- **Delivered quantity:** Not stored. A line is delivered iff its shipment is `DELIVERED` (shipments are delivered whole; partial refusal is modelled as delivery followed by a return).

#### `shipment_status_history`
Standard structure (1C §2.2), FK `shipment_id`. Lifecycle changes as recorded by **our** system (R12).

#### `shipment_tracking`
Carrier scan events from the carrier's feed (R12). High volume and external, so it's a good source for late-arriving and duplicate events in anomaly mode.

| Column | Type | NN | Notes |
|---|---|---|---|
| `shipment_tracking_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `shipment_id` | `BIGINT UNSIGNED` | ✓ | FK → `shipments` |
| `carrier_event_id` | `VARCHAR(40)` | ✓ | UNIQUE. The carrier's own event id; the natural dedupe key |
| `event_sequence` | `SMALLINT UNSIGNED` | ✓ | Order of events within the shipment |
| `event_code` | `VARCHAR(30)` | ✓ | `PICKED_UP` / `ARRIVED_AT_HUB` / `DEPARTED_HUB` / `IN_TRANSIT` / `OUT_FOR_DELIVERY` / `DELIVERY_ATTEMPTED` / `DELIVERED` / `RTO_INITIATED` / `RTO_IN_TRANSIT` / `RTO_DELIVERED` |
| `event_description` | `VARCHAR(150)` | ✓ | Carrier text ("Arrived at Nagpur Hub") |
| `event_at` | `DATETIME(6)` | ✓ | Scan time at the carrier |
| `received_at` | `DATETIME(6)` | ✓ | When the event reached our system (feed lag) |. The row's `created_at` = `received_at` (Part 1H F5)
| `location_city` | `VARCHAR(80)` | | Hub city |
| `state_id` | `SMALLINT UNSIGNED` | | FK → `states` |

- **Keys:** UNIQUE `(carrier_event_id)`; UNIQUE `(shipment_id, event_sequence)`.
- **Constraints:** CHECK list; CHECK `received_at >= event_at`.
- **Clean-mode rules:** `event_at` increases with `event_sequence`; `received_at − event_at` ≤ 6 hours. The anomaly mode (Phase 7) breaks these deliberately, in `scm_<code>_sim` only.
- **Indexes:** `(shipment_id, event_at)`, `(received_at)` (watermark for feed-style loading).
- **Populated:** 4–10 events per shipment along a hub route chosen from origin and destination regions. Insert-only.

#### `delivery_attempts`

| Column | Type | NN | Notes |
|---|---|---|---|
| `delivery_attempt_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `shipment_id` | `BIGINT UNSIGNED` | ✓ | FK → `shipments` |
| `attempt_number` | `TINYINT UNSIGNED` | ✓ | 1–3 |
| `attempted_at` | `DATETIME(6)` | ✓ | |
| `attempt_outcome` | `VARCHAR(20)` | ✓ | `DELIVERED` / `FAILED` |
| `failure_reason` | `VARCHAR(30)` | | `CUSTOMER_UNAVAILABLE` / `ADDRESS_NOT_FOUND` / `REFUSED` / `COD_CASH_NOT_READY` / `PREMISES_CLOSED` / `WEATHER_DELAY` |
| `received_by_name` | `VARCHAR(120)` | | Person who signed |
| `proof_of_delivery_reference` | `VARCHAR(40)` | | OTP / signature reference |
| `cod_collected_amount` | `DECIMAL(18,2)` | | COD shipments, delivered attempt |

- **Keys:** UNIQUE `(shipment_id, attempt_number)`.
- **Constraints:** CHECK `attempt_number BETWEEN 1 AND 3`; CHECK `(attempt_outcome = 'FAILED') = (failure_reason IS NOT NULL)`; CHECK `attempt_outcome = 'FAILED' OR received_by_name IS NOT NULL`.
- **Rules:** At most one `DELIVERED` attempt per shipment, and it's the last one. `cod_collected_amount` = shipment `cod_amount` = the COD payment amount. Three failures ⇒ RTO (V-SH-04).

---

## 5. Invoicing Tables

Tax amount pattern: 1C §2.3. Numbering: D38.

#### `invoices`
Tax invoice: one per shipment at dispatch, or one per handover for store sales (D4).

| Column | Type | NN | Notes |
|---|---|---|---|
| `invoice_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `invoice_number` | `VARCHAR(16)` | ✓ | UNIQUE (`KA2627-I000123`) |
| `order_id` | `BIGINT UNSIGNED` | ✓ | FK → `orders` |
| `shipment_id` | `BIGINT UNSIGNED` | | FK → `shipments`, UNIQUE. NULL for handover sales |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK → `customers` |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK. Issuing GSTIN = dispatching / handover warehouse's |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `invoice_date` | `DATE` | ✓ | |
| `issued_at` | `DATETIME(6)` | ✓ | = shipment `dispatched_at`, or the handover time |
| `customer_name` | `VARCHAR(200)` | ✓ | Snapshot (legal requirement, 1A §3) |
| `customer_gstin` | `CHAR(15)` | | Snapshot |
| `billing_address_id` | `BIGINT UNSIGNED` | ✓ | FK → `customer_addresses` (snapshot pointer; versioned addresses keep it valid) |
| `shipping_address_id` | `BIGINT UNSIGNED` | | FK → `customer_addresses` |
| `place_of_supply_state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states`. Ship-to state; store state for handover |
| `is_inter_state` | `BOOLEAN` | ✓ | Place of supply ≠ issuing GSTIN state |
| `payment_term_id` | `SMALLINT UNSIGNED` | ✓ | FK (snapshot) |
| `due_date` | `DATE` | ✓ | `invoice_date + due_days` |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | 1C §2.3, incl. `round_off_amount` |
| `irn` | `CHAR(64)` | | UNIQUE. e-invoice reference (D46) |
| `irn_generated_at` | `DATETIME(6)` | | |
| `invoice_status` | `VARCHAR(30)` | ✓ | `ISSUED` / `CANCELLED` |
| `cancelled_at` | `DATETIME(6)` | | D40 |
| `cancellation_reason` | `VARCHAR(100)` | | |

- **Keys:** Composite FK `(order_id, customer_id)` → `orders`; UNIQUE `(invoice_id, customer_id, company_gst_registration_id)`, the target of the composite FK from `credit_notes` (D80).
- **Constraints:** GSTIN format; CHECK `due_date >= invoice_date`; CHECK `invoice_date = DATE(issued_at)`; CHECK `(irn IS NULL) = (irn_generated_at IS NULL)`; CHECK `customer_gstin IS NOT NULL OR irn IS NULL` (e-invoices only for registered buyers); CHECK `(invoice_status = 'CANCELLED') = (cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)`; CHECK `cancelled_at IS NULL OR cancelled_at <= issued_at + INTERVAL 24 HOUR`.
- **Rules:** The shipment belongs to the order. The issuing GSTIN is the warehouse's. Fulfilment `SHIP` ⇔ `shipment_id` set (V-IV-01). Snapshot values equal the customer master on `issued_at`. Payment status isn't stored here: it lives in `accounts_receivable` (1E).
- **Indexes:** `(customer_id, invoice_date)`, `(invoice_date)`, `(company_gst_registration_id, invoice_date)` (GST returns), `(order_id)`, `(due_date)`.
- **Populated:** In the same transaction as the dispatch (stock issue, reservation fulfilment, order-line counters) or the store handover. ~0.3% cancelled under D40, with the stock issue reversed.

#### `invoice_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `invoice_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `invoice_id` | `BIGINT UNSIGNED` | ✓ | FK → `invoices` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `line_type` | `VARCHAR(20)` | ✓ | `PRODUCT` / `SHIPPING_CHARGE` (D42) |
| `order_line_item_id` | `BIGINT UNSIGNED` | | FK → `order_line_items`. Required for PRODUCT |
| `shipment_line_item_id` | `BIGINT UNSIGNED` | | FK → `shipment_line_items`, UNIQUE. Required for PRODUCT on shipped orders (D48) |
| `product_id` | `BIGINT UNSIGNED` | | FK → `products`. Snapshot for PRODUCT lines, so tax reports don't need a three-table join |
| `hsn_code` | `VARCHAR(8)` | ✓ | Snapshot (legal requirement). SAC `996812` for shipping charges |
| `quantity` | `DECIMAL(18,3)` | ✓ | 1 for SHIPPING_CHARGE |
| `unit_price` … `line_total` | | ✓ | 1C §2.3. Price and discount copied from the order line |

- **Keys:** UNIQUE `(invoice_id, line_number)`; UNIQUE `(shipment_line_item_id)`. Composite FK `(order_line_item_id, product_id)` → `order_line_items` (the line's product is always the order line's); UNIQUE `(invoice_line_item_id, order_line_item_id)`, the target for return lines (D80). For tax-inclusive orders, the invoice recomputes the ex-GST price at the invoice-date rate so the inclusive price is unchanged (D79; amends V-IV-02).
- **Constraints:** CHECK `(line_type = 'PRODUCT') = (order_line_item_id IS NOT NULL AND product_id IS NOT NULL)`; CHECK `line_type = 'PRODUCT' OR shipment_line_item_id IS NULL`; CHECK `quantity > 0`; amount CHECKs (1C §2.3).
- **Rules:** Quantity = the shipment line quantity. Unit price, discount and rate = the order line's (the rate is re-checked against the product rate on the invoice date; if a GST change happened between order and dispatch, the **invoice-date rate** applies and the line differs from the order, which is correct GST behaviour). At most one SHIPPING_CHARGE line per order across its invoices. Product = order line product. A handover sale has exactly one invoice line per order line (V-IV-02).
- **Indexes:** `(order_line_item_id)`, `(product_id)`, `(hsn_code)`.

#### `credit_notes`
GST credit note against one original invoice.

| Column | Type | NN | Notes |
|---|---|---|---|
| `credit_note_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `credit_note_number` | `VARCHAR(16)` | ✓ | UNIQUE (`KA2627-C000045`) |
| `invoice_id` | `BIGINT UNSIGNED` | ✓ | FK → `invoices` (must be ISSUED, not CANCELLED) |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK → `customers` (= the invoice's) |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK (= the invoice's) |
| `credit_note_date` | `DATE` | ✓ | |
| `reason` | `VARCHAR(30)` | ✓ | `SALES_RETURN` / `RTO` / `PRICE_ADJUSTMENT` / `POST_SALE_DISCOUNT` |
| `sales_return_id` | `BIGINT UNSIGNED` | | FK → `sales_returns`. Required iff `SALES_RETURN` |
| `is_inter_state` | `BOOLEAN` | ✓ | = the invoice's (tax is reversed the way it was charged) |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | 1C §2.3 |
| `irn` | `CHAR(64)` | | UNIQUE (D46) |
| `irn_generated_at` | `DATETIME(6)` | | |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `credit_note_status` | `VARCHAR(30)` | ✓ | `ISSUED` / `CANCELLED` |

- **Keys:** Composite FK `(invoice_id, customer_id, company_gst_registration_id)` → `invoices`, so the customer and GSTIN always match the invoice (D80).
- **Constraints:** CHECK `(reason = 'SALES_RETURN') = (sales_return_id IS NOT NULL)`; CHECK lists; CHECK `(irn IS NULL) = (irn_generated_at IS NULL)`.
- **Rules:** `credit_note_date ≥ invoice_date` and ≤ 30 November after the end of the invoice's FY (GST time limit). Totals ≤ the invoice's remaining creditable amount (V-IV-03). `is_inter_state` and GSTIN match the invoice.
- **Indexes:** `(invoice_id)`, `(customer_id, credit_note_date)`, `(company_gst_registration_id, credit_note_date)`.
- **Populated:** After final return inspection (D44), on RTO receipt (D41, full credit of the shipment's invoice), and occasionally for price adjustments or post-sale volume discounts to B2B customers (~1% of B2B invoices).

#### `credit_note_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `credit_note_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `credit_note_id` | `BIGINT UNSIGNED` | ✓ | FK → `credit_notes` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `invoice_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `invoice_line_items` (line of the same invoice) |
| `sales_return_line_item_id` | `BIGINT UNSIGNED` | | FK → `sales_return_line_items` |
| `quantity` | `DECIMAL(18,3)` | ✓ | 0 for value-only adjustments |
| `unit_price` … `line_total` | | ✓ | 1C §2.3; rate = the invoice line's |

- **Keys:** UNIQUE `(credit_note_id, line_number)`; UNIQUE `(credit_note_id, invoice_line_item_id)`.
- **Constraints:** CHECK `quantity >= 0`; CHECK `quantity > 0 OR sales_return_line_item_id IS NULL`; amount CHECKs.
- **Rules:** The invoice line belongs to the credit note's invoice. Σ credited quantity per invoice line ≤ invoiced quantity; Σ credited taxable ≤ invoice-line taxable (V-IV-04). For returns, quantity = the return line's received quantity.

---

## 6. Returns, Refunds & Defective Stock Tables

#### `sales_returns`
Return authorisation (RMA) against one order.

| Column | Type | NN | Notes |
|---|---|---|---|
| `sales_return_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `return_number` | `VARCHAR(30)` | ✓ | UNIQUE (`RMA/<state>/<FY>/<SEQ>`) |
| `order_id` | `BIGINT UNSIGNED` | ✓ | FK → `orders` |
| `requested_at` | `DATETIME(6)` | ✓ | |
| `return_method` | `VARCHAR(30)` | ✓ | `CARRIER_PICKUP` / `DROP_AT_STORE` |
| `return_warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses`. Where goods come back (DC returns zone or store) |
| `shipping_carrier_id` | `SMALLINT UNSIGNED` | | FK. Reverse pickup carrier (`supports_reverse_pickup`) |
| `reverse_tracking_number` | `VARCHAR(30)` | | UNIQUE |
| `return_status` | `VARCHAR(30)` | ✓ | `REQUESTED` / `APPROVED` / `REJECTED` / `PICKUP_SCHEDULED` / `IN_TRANSIT` / `RECEIVED` / `INSPECTED` / `CLOSED` / `CANCELLED` |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (Customer Service) |
| `approved_at` | `DATETIME(6)` | | |
| `rejection_reason` | `VARCHAR(40)` | | `OUTSIDE_RETURN_WINDOW` / `NON_RETURNABLE_PRODUCT` / `NOT_ELIGIBLE` |
| `received_at` | `DATETIME(6)` | | All lines received |
| `closed_at` | `DATETIME(6)` | | Credit note (and refund, if any) completed |

- **Constraints:** CHECK lists; CHECK `(return_status = 'REJECTED') = (rejection_reason IS NOT NULL)`; CHECK `(return_method = 'CARRIER_PICKUP') = (shipping_carrier_id IS NOT NULL)`; CHECK `return_status IN ('REQUESTED','REJECTED','CANCELLED') OR approved_at IS NOT NULL`; CHECK `received_at IS NULL OR received_at > requested_at`.
- **Transitions:** REQUESTED → APPROVED / REJECTED; APPROVED → PICKUP_SCHEDULED → IN_TRANSIT → RECEIVED (or APPROVED → RECEIVED for store drop); RECEIVED → INSPECTED → CLOSED; REQUESTED/APPROVED → CANCELLED.
- **Rules:** `requested_at` within the category `return_window_days` of the line's delivery; products are returnable (V-RT-01).
- **Indexes:** `(order_id)`, `(return_status, requested_at)`.
- **Populated:** For delivered lines, with return probability per pack and category (e.g. Fashion ~15%, Electronics ~5%, Industrial ~1%). Reasons weighted per pack. Partial returns (subset of lines or quantity) are common. ~5% of requests rejected. B2B returns go to the DC; B2C returns to the nearest DC or to a store (drop-off).

#### `sales_return_line_items`
Returned quantity per invoiced line, including physical receipt (R4 absorbs `returned_goods`).

| Column | Type | NN | Notes |
|---|---|---|---|
| `sales_return_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `sales_return_id` | `BIGINT UNSIGNED` | ✓ | FK → `sales_returns` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `order_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `order_line_items` |
| `invoice_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `invoice_line_items`. The billed line being returned (for the credit note and original cost) |
| `return_reason` | `VARCHAR(30)` | ✓ | `DEFECTIVE` / `DAMAGED_IN_TRANSIT` / `WRONG_ITEM` / `NOT_AS_DESCRIBED` / `SIZE_OR_FIT` / `CHANGED_MIND` / `WARRANTY_CLAIM` / `NEAR_EXPIRY` |
| `quantity_requested` | `DECIMAL(18,3)` | ✓ | |
| `quantity_approved` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `quantity_received` | `DECIMAL(18,3)` | ✓ | Default 0 |
| `received_at` | `DATETIME(6)` | | |
| `receiving_location_id` | `BIGINT UNSIGNED` | | FK → `warehouse_locations` (zone RETURNS / QUARANTINE) |
| `line_status` | `VARCHAR(30)` | ✓ | `REQUESTED` / `APPROVED` / `REJECTED` / `RECEIVED` / `INSPECTED` / `CREDITED` |

- **Keys:** UNIQUE `(sales_return_id, line_number)`; UNIQUE `(sales_return_id, invoice_line_item_id)`. Composite FK `(invoice_line_item_id, order_line_item_id)` → `invoice_line_items` (D80).
- **Constraints:** CHECK `quantity_requested > 0`; CHECK `quantity_approved <= quantity_requested`; CHECK `quantity_received <= quantity_approved`; CHECK `(quantity_received > 0) = (received_at IS NOT NULL)`; CHECK lists.
- **Rules:** The invoice line belongs to the order line, and the order line to the return's order. Σ approved per order line ≤ delivered (the DB counter CHECK on `order_line_items` enforces this; V-RT-02). Σ received per invoice line ≤ invoiced quantity. The ledger `RETURN_RECEIPT` = +received into QUARANTINE at the return warehouse, at the original issue cost (1C §4).

#### `return_inspections`
Inspection outcome for received return quantity. More than one per line when the first result is "further inspection".

| Column | Type | NN | Notes |
|---|---|---|---|
| `return_inspection_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `sales_return_line_item_id` | `BIGINT UNSIGNED` | ✓ | FK → `sales_return_line_items` |
| `inspection_sequence` | `TINYINT UNSIGNED` | ✓ | 1, 2, … |
| `inspected_at` | `DATETIME(6)` | ✓ | |
| `inspected_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `quantity_inspected` | `DECIMAL(18,3)` | ✓ | |
| `quantity_sellable` | `DECIMAL(18,3)` | ✓ | → SELLABLE |
| `quantity_defective` | `DECIMAL(18,3)` | ✓ | → DEFECTIVE (+ `defective_stock` row) |
| `quantity_further_inspection` | `DECIMAL(18,3)` | ✓ | Stays in QUARANTINE for the next inspection |
| `inspection_result` | `VARCHAR(30)` | ✓ | `PASSED` / `DEFECTIVE` / `MIXED` / `FURTHER_INSPECTION` |
| `defect_reason` | `VARCHAR(40)` | | `MANUFACTURING_DEFECT` / `PHYSICAL_DAMAGE` / `MISSING_PARTS` / `USED_OR_SOILED` / `EXPIRED` |
| `inspection_notes` | `VARCHAR(255)` | | |

- **Keys:** UNIQUE `(sales_return_line_item_id, inspection_sequence)`.
- **Constraints:** CHECK `quantity_inspected > 0`; CHECK `quantity_sellable + quantity_defective + quantity_further_inspection = quantity_inspected`; CHECK all `>= 0`; CHECK `(quantity_defective > 0) = (defect_reason IS NOT NULL)`; CHECK the result agrees with the quantities.
- **Rules:** Sequence 1 inspects the received quantity; each later sequence inspects exactly the previous `quantity_further_inspection`. Final: Σ sellable + Σ defective = received (V-RT-03). Ledger: two `RETURN_DISPOSITION` rows per bucket moved (−QUARANTINE, +SELLABLE / +DEFECTIVE), source = this inspection.
- **Populated:** 1–3 days after receipt. Outcome probabilities depend on the return reason (e.g. CHANGED_MIND ~90% sellable, DEFECTIVE ~85% defective). ~5% need further inspection.

#### `refunds`
A cash refund paying out (part of) a credit note (R5).

| Column | Type | NN | Notes |
|---|---|---|---|
| `refund_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `refund_number` | `VARCHAR(30)` | ✓ | UNIQUE (`RFD/<state>/<FY>/<SEQ>`) |
| `credit_note_id` | `BIGINT UNSIGNED` | ✓ | FK → `credit_notes` |
| `customer_payment_id` | `BIGINT UNSIGNED` | | FK → `customer_payments`. Original payment refunded to source (cards, UPI); NULL for bank transfer of COD/cash refunds |
| `refund_amount` | `DECIMAL(18,2)` | ✓ | |
| `payment_method_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_methods` |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK → `bank_accounts` |
| `requested_at` | `DATETIME(6)` | ✓ | |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (`approval_limit ≥ amount`) |
| `approved_at` | `DATETIME(6)` | | |
| `processed_at` | `DATETIME(6)` | | |
| `bank_reference` | `VARCHAR(40)` | | |
| `refund_status` | `VARCHAR(30)` | ✓ | `PENDING_APPROVAL` / `APPROVED` / `PROCESSING` / `COMPLETED` / `FAILED` / `REJECTED` |
| `failure_reason` | `VARCHAR(100)` | | |

- **Constraints:** CHECK `refund_amount > 0`; CHECK lists; CHECK `refund_status IN ('PENDING_APPROVAL','REJECTED') OR approved_at IS NOT NULL`; CHECK `(refund_status IN ('COMPLETED','FAILED')) = (processed_at IS NOT NULL)`; CHECK `(refund_status = 'FAILED') = (failure_reason IS NOT NULL)`.
- **Rules:** Σ COMPLETED + in-flight refunds per credit note ≤ the credit note total. The customer actually paid at least that much on the invoice (allocated payments). The payment belongs to the same customer and was SUCCESS (V-RT-04). A FAILED refund is retried as a new refund row.
- **Indexes:** `(credit_note_id)`, `(refund_status, requested_at)`.
- **Populated:** For credit notes on invoices with allocated payments (B2C prepaid / COD / counter). B2B credit notes on unpaid invoices just reduce AR (1E), with no refund. Approval 0–3 days (refunds pending approval at the end of the window are a master-prompt scenario). ~2% failures. Partial refunds when a credit note exceeds what was paid.

#### `defective_stock`
Register of defective quantities: reason, origin and disposition (D45).

| Column | Type | NN | Notes |
|---|---|---|---|
| `defective_stock_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `defect_number` | `VARCHAR(30)` | ✓ | UNIQUE (`DEF/<state>/<FY>/<SEQ>`) |
| `warehouse_id` | `SMALLINT UNSIGNED` | ✓ | FK → `warehouses` |
| `product_id` | `BIGINT UNSIGNED` | ✓ | FK → `products` |
| `inventory_batch_id` | `BIGINT UNSIGNED` | | FK → `inventory_batches` |
| `quantity` | `DECIMAL(18,3)` | ✓ | |
| `return_inspection_id` | `BIGINT UNSIGNED` | | Source FK: customer return |
| `stock_adjustment_line_item_id` | `BIGINT UNSIGNED` | | Source FK: damage found in warehouse |
| `stock_transfer_line_item_id` | `BIGINT UNSIGNED` | | Source FK: damaged in transit |
| `defect_reason` | `VARCHAR(40)` | ✓ | Same list as `return_inspections.defect_reason` + `HANDLING_DAMAGE` / `TRANSIT_DAMAGE` |
| `detected_at` | `DATETIME(6)` | ✓ | |
| `disposition` | `VARCHAR(30)` | ✓ | `PENDING` / `SCRAP` / `RETURN_TO_SUPPLIER` / `REPAIR` |
| `disposition_status` | `VARCHAR(30)` | ✓ | `PENDING` / `COMPLETED` |
| `disposed_at` | `DATETIME(6)` | | |
| `disposed_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` |
| `supplier_id` | `BIGINT UNSIGNED` | | FK → `suppliers`. RETURN_TO_SUPPLIER only (no debit note in v1, R22) |
| `repair_cost_amount` | `DECIMAL(18,2)` | | REPAIR only. Memo estimate of internal repair cost, not posted (D60, Part 1E) |

- **Constraints:** CHECK `quantity > 0`; **exactly one source** CHECK over the three source FKs; CHECK lists; CHECK `(disposition = 'PENDING') = (disposition_status = 'PENDING')`; CHECK `(disposition_status = 'COMPLETED') = (disposed_at IS NOT NULL AND disposed_by_employee_id IS NOT NULL)`; CHECK `(disposition = 'RETURN_TO_SUPPLIER') = (supplier_id IS NOT NULL)`; CHECK `(disposition = 'REPAIR') = (repair_cost_amount IS NOT NULL)`.
- **Rules:** Quantity = the defective quantity produced by the source event. Ledger `DEFECTIVE_DISPOSITION` at completion: SCRAP / RETURN_TO_SUPPLIER = −quantity DEFECTIVE (write-off); REPAIR = −DEFECTIVE / +SELLABLE (V-RT-05). The supplier is an approved supplier of the product.
- **Indexes:** `(warehouse_id, disposition_status)`, `(product_id)`.
- **Populated:** In the same transaction as the source event. Disposition is decided weekly by warehouse managers: Electronics favours REPAIR / RETURN_TO_SUPPLIER, FMCG favours SCRAP. Rules come from the pack.

---

## 7. Relationship Summary

All FKs `RESTRICT` (D15). Forward references to 1E are marked.

| Child | Column(s) | Parent | Card. |
|---|---|---|---|
| `orders` | `customer_id`, `sales_channel_id`, `marketing_channel_id`, `marketing_campaign_id`, `store_warehouse_id`, `billing_address_id`, `shipping_address_id`, `payment_term_id`, `sales_employee_id` | master tables | N:1 |
| `order_line_items` | `order_id` / `product_id` | `orders` / `products` | N:1 |
| `order_status_history` | `order_id` | `orders` | N:1 |
| `customer_payments` | `customer_id`, `order_id`, `payment_method_id`, `bank_account_id`, `collected_by_shipping_carrier_id` | … | N:1 |
| `inventory_reservations` (1C) | `order_line_item_id` | `order_line_items` | N:1 |
| `shipments` | `order_id`, `warehouse_id`, `shipping_address_id`, `shipping_carrier_id` | … | N:1 |
| `shipment_line_items` | `shipment_id` / `order_line_item_id` / `inventory_batch_id` / `picked_from_location_id` | … | N:1 |
| `shipment_status_history`, `shipment_tracking`, `delivery_attempts` | `shipment_id` | `shipments` | N:1 |
| `invoices` | `shipment_id` (UNIQUE) | `shipments` | 0..1:1 |
| `invoices` | `order_id`, `customer_id`, `company_gst_registration_id`, `warehouse_id`, addresses, `place_of_supply_state_id`, `payment_term_id` | … | N:1 |
| `invoice_line_items` | `invoice_id` / `order_line_item_id` / `product_id` | … | N:1 |
| `invoice_line_items` | `shipment_line_item_id` (UNIQUE) | `shipment_line_items` | 0..1:1 |
| `credit_notes` | `invoice_id` / `customer_id` / `company_gst_registration_id` / `sales_return_id` | … | N:1 |
| `credit_note_line_items` | `credit_note_id` / `invoice_line_item_id` / `sales_return_line_item_id` | … | N:1 |
| `sales_returns` | `order_id`, `return_warehouse_id`, `shipping_carrier_id`, `approved_by_employee_id` | … | N:1 |
| `sales_return_line_items` | `sales_return_id` / `order_line_item_id` / `invoice_line_item_id` / `receiving_location_id` | … | N:1 |
| `return_inspections` | `sales_return_line_item_id` | return line | N:1 |
| `refunds` | `credit_note_id` / `customer_payment_id` / `payment_method_id` / `bank_account_id` | … | N:1 |
| `defective_stock` | one of `return_inspection_id` / `stock_adjustment_line_item_id` / `stock_transfer_line_item_id`; `warehouse_id`, `product_id`, `inventory_batch_id`, `supplier_id` | … | N:1 |
| `inventory_transactions` (1C) | `shipment_line_item_id`, `invoice_line_item_id`, `sales_return_line_item_id`, `return_inspection_id`, `defective_stock_id` | tables above | N:1 |
| *(1E)* `accounts_receivable` | invoice / credit note | | 1:1 |
| *(1E)* `customer_payment_allocations` | payment → invoice | | M:N bridge |

**Traceability (master prompt §15A):** `orders` → `order_line_items` → `inventory_reservations` → `shipment_line_items` → `inventory_transactions` (SALES_ISSUE) → `invoice_line_items` → *AR / allocations / `customer_payments`* (1E) → `sales_return_line_items` → `return_inspections` → `inventory_transactions` (RETURN_RECEIPT / DISPOSITION) + `defective_stock` → `credit_note_line_items` → `refunds` → *journal entries* (1E). Every link is a real FK, and each can be walked in either direction.

---

## 8. Change Behaviour and Volumes

| Behaviour | Tables |
|---|---|
| Insert-only | `order_line_items`*, `order_status_history`, `shipment_line_items`, `shipment_status_history`, `shipment_tracking`, `delivery_attempts`, `invoice_line_items`, `credit_note_line_items`, `return_inspections` |
| Insert + status/counter updates | `orders`, `customer_payments`, `shipments`, `invoices` (cancellation only), `credit_notes`, `sales_returns`, `sales_return_line_items`, `refunds`, `defective_stock` |

\* `order_line_items` counters change after insert, so it's mutable for loading purposes (Q5, 1G).

**Indicative volumes (Medium preset, 2 years, ~50,000 orders as in the master prompt's example):**

| Table | Rows |
|---|---|
| `orders` / `order_line_items` / `order_status_history` | ~50,000 / ~125,000 / ~300,000 |
| `customer_payments` | ~62,000 (incl. failed attempts) |
| `shipments` / lines | ~52,000 / ~135,000 |
| `shipment_status_history` / `shipment_tracking` / `delivery_attempts` | ~520,000 / ~350,000 / ~56,000 |
| `invoices` / lines | ~55,000 / ~140,000 |
| `credit_notes` / lines | ~4,500 / ~7,000 |
| `sales_returns` / lines / inspections | ~3,000 / ~4,500 / ~4,800 |
| `refunds` | ~2,500 |
| `defective_stock` | ~1,500 |

---

## 9. Validation Rules (sales, shipping, invoicing, returns)

★ = reconciliation named in the master prompt (§10, §15E).

| ID | Rule |
|---|---|
| ★ V-SO-01 | Order and invoice header amounts = Σ lines (+ shipping, round-off); line tax = rate × taxable; invoice tax split matches `is_inter_state` and the place of supply (master prompt "incorrect invoice totals / tax calculations") |
| V-SO-02 | Order attribution rules (A1): campaign window and channel; sales channel allowed for the customer type; addresses live at order time |
| ★ V-SO-03 | Order-line counters = Σ shipment lines (shipped), delivered shipments (delivered), invoice lines of ISSUED invoices (invoiced), RTO shipments (rto), approved return lines (return requested) |
| ★ V-SO-04 | Orders vs invoices: every shipped or handed-over quantity is invoiced exactly once (net of cancelled invoices); no invoice without a dispatch or handover |
| V-SO-05 | Order status agrees with line statuses and quantities; status-history chains are valid |
| V-SO-06 | Credit hold: no B2B order is CONFIRMED while open AR + order value exceeds the credit limit, unless released by an override recorded in history |
| ★ V-PY-01 | Invoices vs payments: Σ allocated SUCCESS payments + credit notes ≤ invoice total; outstanding = total − allocations − credits (1E) |
| V-PY-02 | Prepaid orders are confirmed only after a SUCCESS payment ≥ the order estimate; COD payments exist exactly for delivered COD shipments, with amount = `cod_amount` |
| V-SH-01 | Shipment timestamps follow the status order; dispatch after order confirmation; delivery after dispatch |
| V-SH-02 | Every dispatched shipment line has exactly one SALES_ISSUE ledger row and one invoice line; cancelled-before-dispatch shipments have neither |
| V-SH-03 | Tracking events are in order with lag ≤ 6 h (clean mode); the last event agrees with the shipment status |
| V-SH-04 | At most 3 attempts; exactly one DELIVERED attempt for delivered shipments; RTO only after 3 failures (or a refusal) |
| V-SH-05 | Σ shipment `freight_cost_amount` per carrier per month = the carrier's freight bill (1E) |
| V-IV-01 | Invoice ↔ shipment ↔ order ↔ warehouse ↔ GSTIN consistency; issuing GSTIN = warehouse GSTIN; snapshots = master data at issue time |
| V-IV-02 | Invoice lines 1:1 with shipment lines (SHIP) or order lines (handover); at most one shipping-charge line per order; invoice-date GST rate used |
| V-IV-03 | Credit notes: same customer, GSTIN and tax type as the invoice; within the GST time limit; totals ≤ the invoice's creditable amount |
| ★ V-IV-04 | Σ credited quantity / taxable per invoice line ≤ invoiced; return credit quantity = received quantity |
| V-IV-05 | Invoice numbers ≤ 16 characters and unique per GSTIN per FY (D38); cancelled invoices cancelled within 24 h and before in-transit (D40) |
| V-RT-01 | Returns requested within the return window of delivery, for returnable products; rejected requests have no received quantity |
| ★ V-RT-02 | Returned quantity ≤ eligible delivered quantity (per order line and per invoice line) |
| ★ V-RT-03 | Returned goods vs inventory: RETURN_RECEIPT = received quantity into QUARANTINE; final inspections move all of it to SELLABLE + DEFECTIVE; defective quantities have `defective_stock` rows |
| ★ V-RT-04 | Returns vs refunds: Σ refunds per credit note ≤ credit note total ≤ customer paid amount (when refunded); refunds reference SUCCESS payments of the same customer |
| V-RT-05 | Defective stock: exactly one source; completed dispositions have matching DEFECTIVE_DISPOSITION ledger rows; pending ones remain in `inventory.qty_defective` |
| V-RT-06 | RTO: every RETURNED_TO_SENDER shipment has an RTO_RECEIPT per line at the original issue cost and an RTO credit note for its invoice |

---

## 10. What Comes Next

Part 1E: expenses, journal entries and lines, GL view and period balances, bank transactions and reconciliation, AR / AP open items and allocations, tax transactions and GST returns. It includes **double-entry posting rules for every business event** in Parts 1C and 1D, Q2 for journals, and Q3.
