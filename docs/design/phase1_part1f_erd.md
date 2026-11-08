# Phase 1 – Part 1F: Entity-Relationship Diagrams

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9). Baseline for Phase 2
**Last updated:** 2026-11-08
**Builds on:** Parts 1A–1E (all table definitions and decisions D1–D63)

---

## 0. How to Read These Diagrams

- The diagrams use **Mermaid** `erDiagram` syntax. GitHub renders them directly. In VS Code, install a Mermaid preview extension (e.g. "Markdown Preview Mermaid Support") and open the Markdown preview.
- The database is split into **12 module diagrams** plus an overview. Each module diagram lists **keys only** (PK, FK, UK) and a few defining columns for the tables it owns. Full column definitions are in Parts 1B–1E (the "Defined in" column of §1).
- A table owned by another module appears **without an attribute block**, as a reference stub, so cross-module links stay visible without repeating definitions.
- **Cardinality notation** (crow's foot): `||` exactly one, `|o` zero or one, `|{` one or more, `o{` zero or more. The left marker counts the left table per one row of the right table, and the right marker counts the right table per one row of the left. `customers ||--o{ orders` = every order has exactly one customer; a customer has zero or more orders.
- FK names are only shown when the role isn't obvious (e.g. `store_warehouse_id`). Every FK is `ON DELETE/UPDATE RESTRICT` (D15).
- "Exactly one source FK" relationships (D28, D45, D50, D51) are drawn as several optional (`|o`) links. The CHECK constraint that exactly one is set is noted in the label as "(one of)".

---

## 1. Table Index (92 tables + 1 view)

| Module | Tables | Diagram | Defined in |
|---|---|---|---|
| Company, organisation & locations | `states`, `companies`, `company_gst_registrations`, `business_units`, `departments`, `employees`, `warehouses`, `warehouse_locations`, `bank_accounts`, `cost_centers` | §3 | 1B |
| Parties, catalogue & commercial reference | `customers`, `customer_addresses`, `suppliers`, `supplier_addresses`, `supplier_products`, `product_categories`, `product_brands`, `product_units_of_measure`, `products`, `product_tax_rates`, `payment_methods`, `payment_terms`, `shipping_carriers`, `expense_categories`, `sales_channels`, `marketing_channels`, `marketing_campaigns` | §4 | 1B |
| Accounting master | `account_types`, `chart_of_accounts`, `accounting_periods`, `tax_accounts` | §5 | 1B |
| Procurement | `rfq_headers`, `rfq_line_items`, `rfq_suppliers`, `rfq_status_history`, `quotation_headers`, `quotation_line_items`, `quotation_status_history`, `purchase_order_headers`, `purchase_order_line_items`, `purchase_order_status_history`, `goods_receipts`, `goods_receipt_line_items`, `goods_inspections`, `supplier_invoices`, `supplier_invoice_line_items`, `supplier_payments` | §6 | 1C |
| Inventory documents & balances | `inventory`, `inventory_batches`, `inventory_reservations`, `stock_adjustments`, `stock_adjustment_line_items`, `stock_transfers`, `stock_transfer_line_items` | §7 | 1C |
| Inventory ledger | `inventory_transactions` | §8 | 1C |
| Sales & shipping | `orders`, `order_line_items`, `order_status_history`, `customer_payments`, `shipments`, `shipment_line_items`, `shipment_status_history`, `shipment_tracking`, `delivery_attempts` | §9 | 1D |
| Invoicing & returns | `invoices`, `invoice_line_items`, `credit_notes`, `credit_note_line_items`, `sales_returns`, `sales_return_line_items`, `return_inspections`, `refunds`, `defective_stock` | §10 | 1D |
| Expenses | `expenses`, `expense_line_items`, `expense_payments`, `expense_attachments_metadata` | §11 | 1E |
| Journals, ledger & bank | `journal_entries`, `journal_entry_lines`, `gl_period_balances`, `bank_transactions`, `bank_reconciliations`, view `v_general_ledger` | §12, §13 | 1E |
| Receivables, payables & tax | `accounts_receivable`, `customer_payment_allocations`, `accounts_payable`, `supplier_payment_allocations`, `tax_transactions`, `gst_returns`, `gst_return_line_items` | §14 | 1E |
| System & generation control | `document_sequences`, `generation_runs`, `generation_run_steps` | §15 | 1G (keys shown here) |

Count: 10 + 17 + 4 + 16 + 7 + 1 + 9 + 9 + 4 + 5 + 7 + 3 = **92**. Outside the workspace schema: `scm_control.workspaces` (platform registry, A2) and the DuckDB `etl_*` metadata tables (1G).

---

## 2. Overview: Modules and Business Flow

```mermaid
flowchart LR
    MD["Master data<br/>company · org · parties · catalogue"]
    AM["Accounting master<br/>COA · periods · cost centres · tax accounts"]
    PR["Procurement<br/>RFQ → quotation → PO → GRN → supplier invoice → payment"]
    IN["Inventory<br/>balances · batches · reservations · adjustments · transfers"]
    LG["Inventory ledger<br/>inventory_transactions"]
    SA["Sales & shipping<br/>orders · payments · shipments · tracking"]
    IV["Invoicing & returns<br/>invoices · credit notes · returns · refunds · defective"]
    EX["Expenses<br/>bills · claims · petty cash · payroll"]
    JE["Journals & ledger<br/>journal entries · GL view · period balances"]
    BK["Bank<br/>statement lines · reconciliations"]
    SL["AR / AP / GST<br/>open items · allocations · tax transactions · returns"]

    MD --> PR & SA & EX & IN
    AM --> JE & SL
    PR -- "GRN accepted qty" --> LG
    SA -- "reservations" --> IN
    SA -- "dispatch" --> LG
    IV -- "returns · RTO · disposition" --> LG
    IN -- "adjustments · transfers" --> LG
    LG -- "balances" --> IN
    SA --> IV
    PR -- "posting" --> JE
    IV -- "posting incl. COGS" --> JE
    SA -- "receipts" --> JE
    IN -- "posting" --> JE
    EX -- "posting" --> JE
    BK -- "settlements · charges" --> JE
    JE --> SL
    PR & IV & EX --> SL
    SA & PR & EX & IV -- "cash movements" --> BK
```

**Reading the overview:** master data feeds every operational module. Every quantity change goes through the single inventory ledger (D28/D33). Every financially relevant event posts exactly one journal (D50). The AR/AP and GST subledgers are built from documents and their journals, and reconcile to the GL control accounts (1E §7).

---

## 3. Company, Organisation & Locations

```mermaid
erDiagram
    states {
        smallint state_id PK
        char gst_state_code UK
        char state_code UK
        varchar intra_state_tax_component
    }
    companies {
        smallint company_id PK
        varchar company_code UK
        char cin UK
        char pan UK
        smallint registered_state_id FK
        char base_currency_code
    }
    company_gst_registrations {
        smallint company_gst_registration_id PK
        smallint company_id FK
        smallint state_id FK
        char gstin UK
        boolean is_principal_place
    }
    business_units {
        smallint business_unit_id PK
        varchar business_unit_code UK
        int revenue_account_id FK
        int sales_returns_account_id FK
        int cogs_account_id FK
    }
    departments {
        smallint department_id PK
        varchar department_code UK
    }
    employees {
        bigint employee_id PK
        varchar employee_code UK
        smallint department_id FK
        smallint warehouse_id FK
        smallint cost_center_id FK
        bigint manager_employee_id FK
        varchar job_level
        varchar employment_status
    }
    warehouses {
        smallint warehouse_id PK
        varchar warehouse_code UK
        varchar warehouse_type
        smallint company_gst_registration_id FK "composite FK with state_id"
        smallint state_id FK
        smallint replenishment_warehouse_id FK
    }
    warehouse_locations {
        bigint warehouse_location_id PK
        smallint warehouse_id FK
        varchar location_code "UK with warehouse_id"
        varchar zone
    }
    bank_accounts {
        smallint bank_account_id PK
        varchar account_code UK
        varchar account_type
        smallint warehouse_id FK "petty cash only"
        int gl_account_id FK, UK
    }
    cost_centers {
        smallint cost_center_id PK
        varchar cost_center_code UK
        varchar cost_center_type
        smallint business_unit_id FK, UK
        smallint warehouse_id FK, UK
        smallint department_id FK, UK
        smallint parent_cost_center_id FK
    }

    states ||--o{ companies : "registered in"
    companies ||--|{ company_gst_registrations : "holds"
    states ||--o{ company_gst_registrations : "state of"
    company_gst_registrations ||--|{ warehouses : "registers (same state)"
    states ||--o{ warehouses : "located in"
    warehouses |o--o{ warehouses : "replenishes (DC to store)"
    warehouses ||--|{ warehouse_locations : "contains"
    warehouses |o--o{ bank_accounts : "petty cash float"
    departments ||--o{ employees : "employs"
    warehouses |o--o{ employees : "work location"
    cost_centers ||--o{ employees : "salary charged to"
    employees |o--o{ employees : "manages"
    business_units |o--|| cost_centers : "has (type BU)"
    warehouses |o--|| cost_centers : "has (type WAREHOUSE)"
    departments |o--|| cost_centers : "has (type DEPARTMENT)"
    cost_centers |o--o{ cost_centers : "parent of"
```

---

## 4. Parties, Catalogue & Commercial Reference

```mermaid
erDiagram
    customers {
        bigint customer_id PK
        varchar customer_code UK
        varchar customer_type
        char gstin UK
        smallint payment_term_id FK
        bigint sales_employee_id FK
        smallint acquisition_marketing_channel_id FK
        bigint acquisition_marketing_campaign_id FK
    }
    customer_addresses {
        bigint customer_address_id PK
        bigint customer_id FK
        smallint state_id FK
        bigint previous_customer_address_id FK, UK
        bigint default_billing_key UK "generated"
        bigint default_shipping_key UK "generated"
    }
    suppliers {
        bigint supplier_id PK
        varchar supplier_code UK
        varchar supplier_type
        char gstin UK
        smallint payment_term_id FK
        smallint default_expense_category_id FK
    }
    supplier_addresses {
        bigint supplier_address_id PK
        bigint supplier_id FK
        smallint state_id FK
        bigint previous_supplier_address_id FK, UK
        varchar default_key UK "generated"
    }
    supplier_products {
        bigint supplier_product_id PK
        bigint supplier_id FK "UK with product_id"
        bigint product_id FK
        decimal reference_unit_cost
        bigint preferred_key UK "generated"
    }
    product_categories {
        int product_category_id PK
        varchar category_code UK
        int parent_category_id FK "composite with business_unit_id"
        smallint business_unit_id FK
    }
    product_brands {
        int product_brand_id PK
        varchar brand_code UK
    }
    product_units_of_measure {
        smallint unit_of_measure_id PK
        varchar uom_code UK
    }
    products {
        bigint product_id PK
        varchar sku UK
        smallint business_unit_id FK
        int product_category_id FK "composite with business_unit_id"
        int product_brand_id FK
        smallint base_unit_of_measure_id FK
        varchar hsn_code
        decimal list_price
    }
    product_tax_rates {
        bigint product_tax_rate_id PK
        bigint product_id FK "UK with effective_from"
        decimal gst_rate
        date effective_from
        date effective_to
        bigint current_key UK "generated"
    }
    payment_terms {
        smallint payment_term_id PK
        varchar term_code UK
    }
    payment_methods {
        smallint payment_method_id PK
        varchar method_code UK
    }
    shipping_carriers {
        smallint shipping_carrier_id PK
        varchar carrier_code UK
        bigint supplier_id FK, UK
    }
    expense_categories {
        smallint expense_category_id PK
        varchar category_code UK
        int gl_account_id FK
    }
    sales_channels {
        smallint sales_channel_id PK
        varchar channel_code UK
    }
    marketing_channels {
        smallint marketing_channel_id PK
        varchar channel_code UK
    }
    marketing_campaigns {
        bigint marketing_campaign_id PK
        varchar campaign_code UK
        smallint marketing_channel_id FK
        smallint business_unit_id FK
        bigint owner_employee_id FK
    }

    customers ||--|{ customer_addresses : "has"
    customer_addresses |o--o| customer_addresses : "replaces (version)"
    states ||--o{ customer_addresses : "in"
    payment_terms ||--o{ customers : "default terms"
    employees |o--o{ customers : "account manager"
    marketing_channels ||--o{ customers : "acquired via (first touch)"
    marketing_campaigns |o--o{ customers : "acquired by"
    suppliers ||--|{ supplier_addresses : "has"
    supplier_addresses |o--o| supplier_addresses : "replaces (version)"
    states ||--o{ supplier_addresses : "in"
    payment_terms ||--o{ suppliers : "default terms"
    expense_categories |o--o{ suppliers : "default category"
    suppliers ||--o{ supplier_products : "approved for"
    products ||--o{ supplier_products : "supplied by"
    suppliers |o--o| shipping_carriers : "bills freight as"
    business_units ||--o{ product_categories : "owns"
    product_categories |o--o{ product_categories : "parent of"
    product_categories ||--o{ products : "classifies (leaf)"
    business_units ||--o{ products : "owns"
    product_brands ||--o{ products : "brands"
    product_units_of_measure ||--o{ products : "base UoM"
    products ||--|{ product_tax_rates : "GST rate history"
    marketing_channels ||--o{ marketing_campaigns : "runs on"
    business_units |o--o{ marketing_campaigns : "targets"
    employees ||--o{ marketing_campaigns : "owns"
```

`sales_channels` and `payment_methods` have no FKs of their own; they're referenced by orders, payments and refunds (§9, §10).

---

## 5. Accounting Master

```mermaid
erDiagram
    account_types {
        tinyint account_type_id PK
        varchar account_type_code UK
        varchar normal_balance
        char code_prefix UK
    }
    chart_of_accounts {
        int account_id PK
        varchar account_code UK
        tinyint account_type_id FK
        int parent_account_id FK "composite with account_type_id"
        varchar system_role UK
        boolean is_postable
        boolean is_control_account
    }
    accounting_periods {
        smallint accounting_period_id PK
        char period_code UK
        smallint fiscal_year "UK with period_number"
        varchar period_status
        bigint closed_by_employee_id FK
    }
    tax_accounts {
        smallint tax_account_id PK
        smallint company_gst_registration_id FK "UK with component, direction"
        varchar tax_component
        varchar tax_direction
        int gl_account_id FK, UK
    }

    account_types ||--o{ chart_of_accounts : "classifies"
    chart_of_accounts |o--o{ chart_of_accounts : "parent of (same type)"
    chart_of_accounts ||--o| business_units : "revenue / returns / COGS account"
    chart_of_accounts ||--o{ expense_categories : "GL account"
    chart_of_accounts ||--o| bank_accounts : "GL account"
    chart_of_accounts ||--o| tax_accounts : "GL account"
    company_gst_registrations ||--|{ tax_accounts : "6 per GSTIN"
    employees |o--o{ accounting_periods : "closes"
```

`business_units` has three separate FKs to `chart_of_accounts` (revenue, sales returns, COGS), drawn as one link. Cost centres are in §3.

---

## 6. Procurement

```mermaid
erDiagram
    rfq_headers {
        bigint rfq_id PK
        varchar rfq_number UK
        smallint delivery_warehouse_id FK
        smallint business_unit_id FK
        bigint created_by_employee_id FK
        varchar rfq_status
    }
    rfq_line_items {
        bigint rfq_line_item_id PK
        bigint rfq_id FK "UK with line_number; UK with product_id"
        bigint product_id FK
        decimal requested_quantity
    }
    rfq_suppliers {
        bigint rfq_supplier_id PK
        bigint rfq_id FK "UK with supplier_id"
        bigint supplier_id FK
        varchar response_status
    }
    rfq_status_history {
        bigint rfq_status_history_id PK
        bigint rfq_id FK
        bigint changed_by_employee_id FK
    }
    quotation_headers {
        bigint quotation_id PK
        varchar quotation_number UK
        bigint rfq_supplier_id FK, UK
        smallint payment_term_id FK
        bigint evaluated_by_employee_id FK
        varchar quotation_status
    }
    quotation_line_items {
        bigint quotation_line_item_id PK
        bigint quotation_id FK "UK with rfq_line_item_id"
        bigint rfq_line_item_id FK
        bigint awarded_rfq_line_key UK "generated"
    }
    quotation_status_history {
        bigint quotation_status_history_id PK
        bigint quotation_id FK
    }
    purchase_order_headers {
        bigint purchase_order_id PK
        varchar po_number UK
        varchar po_source
        bigint quotation_id FK, UK
        bigint supplier_id FK
        bigint supplier_address_id FK
        smallint delivery_warehouse_id FK "UK with purchase_order_id"
        smallint payment_term_id FK
        bigint approved_by_employee_id FK
        varchar po_status
    }
    purchase_order_line_items {
        bigint purchase_order_line_item_id PK
        bigint purchase_order_id FK "UK with product_id"
        bigint product_id FK
        bigint quotation_line_item_id FK, UK
        decimal quantity_open "generated"
    }
    purchase_order_status_history {
        bigint purchase_order_status_history_id PK
        bigint purchase_order_id FK
    }
    goods_receipts {
        bigint goods_receipt_id PK
        varchar grn_number UK
        bigint purchase_order_id FK "composite with warehouse_id"
        smallint warehouse_id FK
        bigint received_by_employee_id FK
    }
    goods_receipt_line_items {
        bigint goods_receipt_line_item_id PK
        bigint goods_receipt_id FK "UK with purchase_order_line_item_id"
        bigint purchase_order_line_item_id FK
        bigint receiving_location_id FK
    }
    goods_inspections {
        bigint goods_inspection_id PK
        bigint goods_receipt_line_item_id FK, UK
        decimal quantity_accepted
        decimal quantity_rejected
    }
    supplier_invoices {
        bigint supplier_invoice_id PK
        varchar internal_reference UK
        bigint supplier_id FK "UK with supplier_invoice_number"
        bigint purchase_order_id FK
        smallint company_gst_registration_id FK
        varchar invoice_status
    }
    supplier_invoice_line_items {
        bigint supplier_invoice_line_item_id PK
        bigint supplier_invoice_id FK "UK with purchase_order_line_item_id"
        bigint purchase_order_line_item_id FK
    }
    supplier_payments {
        bigint supplier_payment_id PK
        varchar payment_number UK
        bigint supplier_id FK
        smallint bank_account_id FK
        smallint payment_method_id FK
        varchar payment_status
    }

    warehouses ||--o{ rfq_headers : "delivery to"
    rfq_headers ||--|{ rfq_line_items : "requests"
    products ||--o{ rfq_line_items : "requested"
    rfq_headers ||--|{ rfq_suppliers : "invites"
    suppliers ||--o{ rfq_suppliers : "invited"
    rfq_headers ||--|{ rfq_status_history : "lifecycle"
    rfq_suppliers ||--o| quotation_headers : "answered by"
    quotation_headers ||--|{ quotation_line_items : "quotes"
    rfq_line_items ||--o{ quotation_line_items : "quoted in"
    quotation_headers ||--|{ quotation_status_history : "lifecycle"
    quotation_headers |o--o| purchase_order_headers : "awarded as (RFQ_AWARD)"
    quotation_line_items |o--o| purchase_order_line_items : "awarded line"
    suppliers ||--o{ purchase_order_headers : "ordered from"
    supplier_addresses ||--o{ purchase_order_headers : "dispatch from"
    purchase_order_headers ||--|{ purchase_order_line_items : "orders"
    products ||--o{ purchase_order_line_items : "ordered"
    purchase_order_headers ||--|{ purchase_order_status_history : "lifecycle"
    purchase_order_headers ||--o{ goods_receipts : "delivered in"
    goods_receipts ||--|{ goods_receipt_line_items : "contains"
    purchase_order_line_items ||--o{ goods_receipt_line_items : "received in"
    warehouse_locations ||--o{ goods_receipt_line_items : "received at"
    goods_receipt_line_items ||--o| goods_inspections : "inspected by"
    purchase_order_headers ||--o{ supplier_invoices : "billed by"
    suppliers ||--o{ supplier_invoices : "issues"
    company_gst_registrations ||--o{ supplier_invoices : "billed to"
    supplier_invoices ||--|{ supplier_invoice_line_items : "bills"
    purchase_order_line_items ||--o{ supplier_invoice_line_items : "matched (3-way)"
    suppliers ||--o{ supplier_payments : "paid"
    bank_accounts ||--o{ supplier_payments : "paid from"
    payment_methods ||--o{ supplier_payments : "method"
```

Supplier payments are applied to invoices through `supplier_payment_allocations` (§14).

---

## 7. Inventory Documents and Balances

```mermaid
erDiagram
    inventory {
        bigint inventory_id PK
        smallint warehouse_id FK "UK with product_id"
        bigint product_id FK
        decimal qty_sellable
        decimal qty_quarantine
        decimal qty_defective
        decimal qty_reserved
        decimal stock_value
        decimal moving_average_cost "generated"
    }
    inventory_batches {
        bigint inventory_batch_id PK
        varchar batch_number UK
        bigint product_id FK
        bigint goods_receipt_line_item_id FK, UK "one of"
        bigint stock_adjustment_line_item_id FK, UK "one of"
        date expiry_date
    }
    inventory_reservations {
        bigint inventory_reservation_id PK
        bigint order_line_item_id FK "UK with warehouse_id"
        smallint warehouse_id FK
        decimal outstanding_quantity "generated"
    }
    stock_adjustments {
        bigint stock_adjustment_id PK
        varchar adjustment_number UK
        smallint warehouse_id FK
        varchar adjustment_type
        bigint approved_by_employee_id FK
    }
    stock_adjustment_line_items {
        bigint stock_adjustment_line_item_id PK
        bigint stock_adjustment_id FK
        bigint product_id FK
        bigint inventory_batch_id FK
        varchar stock_status
    }
    stock_transfers {
        bigint stock_transfer_id PK
        varchar transfer_number UK
        smallint source_warehouse_id FK
        smallint destination_warehouse_id FK
        varchar tax_invoice_number UK
        smallint shipping_carrier_id FK
    }
    stock_transfer_line_items {
        bigint stock_transfer_line_item_id PK
        bigint stock_transfer_id FK
        bigint product_id FK
        bigint inventory_batch_id FK
    }

    warehouses ||--o{ inventory : "holds"
    products ||--o{ inventory : "stocked as"
    products ||--o{ inventory_batches : "lots of"
    goods_receipt_line_items |o--o| inventory_batches : "creates (one of)"
    stock_adjustment_line_items |o--o| inventory_batches : "creates (one of)"
    order_line_items ||--o{ inventory_reservations : "reserved by"
    warehouses ||--o{ inventory_reservations : "reserved at"
    warehouses ||--o{ stock_adjustments : "adjusted at"
    stock_adjustments ||--|{ stock_adjustment_line_items : "contains"
    products ||--o{ stock_adjustment_line_items : "adjusted"
    inventory_batches |o--o{ stock_adjustment_line_items : "batch adjusted"
    warehouses ||--o{ stock_transfers : "source"
    warehouses ||--o{ stock_transfers : "destination"
    shipping_carriers |o--o{ stock_transfers : "carried by"
    stock_transfers ||--|{ stock_transfer_line_items : "contains"
    products ||--o{ stock_transfer_line_items : "transferred"
    inventory_batches |o--o{ stock_transfer_line_items : "batch transferred"
```

---

## 8. Inventory Ledger: Movement Sources

`inventory_transactions` is the only movement ledger. Each row has **exactly one** source document FK (D28), and the source must match `transaction_type` (1C §5.0, 1D D41).

```mermaid
erDiagram
    inventory_transactions {
        bigint inventory_transaction_id PK
        datetime transaction_at
        smallint warehouse_id FK
        bigint product_id FK
        bigint inventory_batch_id FK
        bigint warehouse_location_id FK
        varchar transaction_type
        varchar stock_status
        decimal quantity "signed"
        decimal value_change "signed"
        bigint goods_receipt_line_item_id FK "one of"
        bigint shipment_line_item_id FK "one of"
        bigint invoice_line_item_id FK "one of"
        bigint sales_return_line_item_id FK "one of"
        bigint return_inspection_id FK "one of"
        bigint stock_transfer_line_item_id FK "one of"
        bigint stock_adjustment_line_item_id FK "one of"
        bigint defective_stock_id FK "one of"
        bigint reversal_of_inventory_transaction_id FK, UK
        bigint generation_run_id FK
    }

    warehouses ||--o{ inventory_transactions : "at"
    products ||--o{ inventory_transactions : "of"
    inventory_batches |o--o{ inventory_transactions : "batch"
    warehouse_locations |o--o{ inventory_transactions : "location"
    goods_receipt_line_items |o--o{ inventory_transactions : "PURCHASE_RECEIPT"
    shipment_line_items |o--o{ inventory_transactions : "SALES_ISSUE / RTO_RECEIPT"
    invoice_line_items |o--o{ inventory_transactions : "SALES_ISSUE (handover)"
    sales_return_line_items |o--o{ inventory_transactions : "RETURN_RECEIPT"
    return_inspections |o--o{ inventory_transactions : "RETURN_DISPOSITION"
    stock_transfer_line_items |o--o{ inventory_transactions : "TRANSFER_OUT / IN"
    stock_adjustment_line_items |o--o{ inventory_transactions : "ADJUSTMENT"
    defective_stock |o--o{ inventory_transactions : "DEFECTIVE_DISPOSITION"
    inventory_transactions |o--o| inventory_transactions : "reverses"
    generation_runs ||--o{ inventory_transactions : "written by"
```

Balances in `inventory` (§7) always equal the ledger summed per warehouse × product × status (V-IN-01).

---

## 9. Sales & Shipping

```mermaid
erDiagram
    orders {
        bigint order_id PK
        varchar order_number UK
        bigint customer_id FK
        smallint sales_channel_id FK
        smallint marketing_channel_id FK
        bigint marketing_campaign_id FK
        varchar fulfilment_type
        smallint store_warehouse_id FK
        bigint billing_address_id FK
        bigint shipping_address_id FK
        smallint payment_term_id FK
        bigint sales_employee_id FK
        varchar order_status
    }
    order_line_items {
        bigint order_line_item_id PK
        bigint order_id FK "UK with product_id"
        bigint product_id FK
        decimal quantity_ordered
        decimal quantity_returnable "generated"
    }
    order_status_history {
        bigint order_status_history_id PK
        bigint order_id FK
    }
    customer_payments {
        bigint customer_payment_id PK
        varchar payment_number UK
        bigint customer_id FK
        bigint order_id FK "UK with attempt_number"
        smallint payment_method_id FK
        smallint bank_account_id FK
        smallint collected_by_shipping_carrier_id FK
        bigint settlement_bank_transaction_id FK
        varchar payment_status
    }
    shipments {
        bigint shipment_id PK
        varchar shipment_number UK
        bigint order_id FK
        smallint warehouse_id FK
        bigint shipping_address_id FK
        smallint shipping_carrier_id FK
        varchar tracking_number UK
        varchar shipment_status
    }
    shipment_line_items {
        bigint shipment_line_item_id PK
        bigint shipment_id FK
        bigint order_line_item_id FK
        bigint inventory_batch_id FK
        bigint picked_from_location_id FK
    }
    shipment_status_history {
        bigint shipment_status_history_id PK
        bigint shipment_id FK
    }
    shipment_tracking {
        bigint shipment_tracking_id PK
        bigint shipment_id FK "UK with event_sequence"
        varchar carrier_event_id UK
        smallint state_id FK
        datetime event_at
        datetime received_at
    }
    delivery_attempts {
        bigint delivery_attempt_id PK
        bigint shipment_id FK "UK with attempt_number"
        varchar attempt_outcome
    }

    customers ||--o{ orders : "places"
    sales_channels ||--o{ orders : "placed via"
    marketing_channels ||--o{ orders : "attributed to (last touch)"
    marketing_campaigns |o--o{ orders : "driven by"
    warehouses |o--o{ orders : "store (pickup / counter)"
    customer_addresses ||--o{ orders : "billing / shipping"
    payment_terms ||--o{ orders : "terms"
    orders ||--|{ order_line_items : "contains"
    products ||--o{ order_line_items : "ordered"
    orders ||--|{ order_status_history : "lifecycle"
    customers ||--o{ customer_payments : "pays"
    orders |o--o{ customer_payments : "prepaid / COD / counter"
    payment_methods ||--o{ customer_payments : "method"
    bank_accounts ||--o{ customer_payments : "into"
    shipping_carriers |o--o{ customer_payments : "COD collected by"
    bank_transactions |o--o{ customer_payments : "settles (clearing)"
    orders ||--o{ shipments : "fulfilled by"
    warehouses ||--o{ shipments : "dispatched from"
    shipping_carriers ||--o{ shipments : "carried by"
    customer_addresses ||--o{ shipments : "delivered to"
    shipments ||--|{ shipment_line_items : "contains"
    order_line_items ||--o{ shipment_line_items : "shipped in"
    inventory_batches |o--o{ shipment_line_items : "batch"
    shipments ||--|{ shipment_status_history : "lifecycle"
    shipments ||--o{ shipment_tracking : "carrier scans"
    states |o--o{ shipment_tracking : "scan state"
    shipments ||--o{ delivery_attempts : "attempted"
```

---

## 10. Invoicing, Returns & Defective Stock

```mermaid
erDiagram
    invoices {
        bigint invoice_id PK
        varchar invoice_number UK "max 16 chars"
        bigint order_id FK
        bigint shipment_id FK, UK
        bigint customer_id FK
        smallint company_gst_registration_id FK
        smallint warehouse_id FK
        smallint place_of_supply_state_id FK
        char irn UK
        varchar invoice_status
    }
    invoice_line_items {
        bigint invoice_line_item_id PK
        bigint invoice_id FK
        varchar line_type
        bigint order_line_item_id FK
        bigint shipment_line_item_id FK, UK
        bigint product_id FK
    }
    credit_notes {
        bigint credit_note_id PK
        varchar credit_note_number UK "max 16 chars"
        bigint invoice_id FK
        bigint customer_id FK
        smallint company_gst_registration_id FK
        bigint sales_return_id FK
        varchar reason
    }
    credit_note_line_items {
        bigint credit_note_line_item_id PK
        bigint credit_note_id FK "UK with invoice_line_item_id"
        bigint invoice_line_item_id FK
        bigint sales_return_line_item_id FK
    }
    sales_returns {
        bigint sales_return_id PK
        varchar return_number UK
        bigint order_id FK
        smallint return_warehouse_id FK
        smallint shipping_carrier_id FK
        varchar reverse_tracking_number UK
        varchar return_status
    }
    sales_return_line_items {
        bigint sales_return_line_item_id PK
        bigint sales_return_id FK "UK with invoice_line_item_id"
        bigint order_line_item_id FK
        bigint invoice_line_item_id FK
        bigint receiving_location_id FK
    }
    return_inspections {
        bigint return_inspection_id PK
        bigint sales_return_line_item_id FK "UK with inspection_sequence"
        bigint inspected_by_employee_id FK
    }
    refunds {
        bigint refund_id PK
        varchar refund_number UK
        bigint credit_note_id FK
        bigint customer_payment_id FK
        smallint payment_method_id FK
        smallint bank_account_id FK
        varchar refund_status
    }
    defective_stock {
        bigint defective_stock_id PK
        varchar defect_number UK
        smallint warehouse_id FK
        bigint product_id FK
        bigint inventory_batch_id FK
        bigint return_inspection_id FK "one of"
        bigint stock_adjustment_line_item_id FK "one of"
        bigint stock_transfer_line_item_id FK "one of"
        bigint supplier_id FK
        varchar disposition
    }

    orders ||--o{ invoices : "invoiced by"
    shipments |o--o| invoices : "one invoice per shipment"
    customers ||--o{ invoices : "billed"
    company_gst_registrations ||--o{ invoices : "issued under"
    warehouses ||--o{ invoices : "issued at"
    states ||--o{ invoices : "place of supply"
    invoices ||--|{ invoice_line_items : "contains"
    order_line_items |o--o{ invoice_line_items : "invoices"
    shipment_line_items |o--o| invoice_line_items : "1:1 when shipped"
    products |o--o{ invoice_line_items : "product"
    invoices ||--o{ credit_notes : "credited by"
    sales_returns |o--o{ credit_notes : "credited for"
    credit_notes ||--|{ credit_note_line_items : "contains"
    invoice_line_items ||--o{ credit_note_line_items : "credited"
    sales_return_line_items |o--o{ credit_note_line_items : "credited return"
    orders ||--o{ sales_returns : "returned against"
    warehouses ||--o{ sales_returns : "returned to"
    shipping_carriers |o--o{ sales_returns : "reverse pickup"
    sales_returns ||--|{ sales_return_line_items : "contains"
    order_line_items ||--o{ sales_return_line_items : "returned"
    invoice_line_items ||--o{ sales_return_line_items : "billed line returned"
    sales_return_line_items ||--o{ return_inspections : "inspected"
    credit_notes ||--o{ refunds : "paid out by"
    customer_payments |o--o{ refunds : "refunded to source"
    return_inspections |o--o{ defective_stock : "found defective (one of)"
    stock_adjustment_line_items |o--o{ defective_stock : "warehouse damage (one of)"
    stock_transfer_line_items |o--o{ defective_stock : "transit damage (one of)"
    warehouses ||--o{ defective_stock : "held at"
    products ||--o{ defective_stock : "of"
    suppliers |o--o{ defective_stock : "returned to supplier"
```

---

## 11. Expenses

```mermaid
erDiagram
    expenses {
        bigint expense_id PK
        varchar expense_number UK
        varchar expense_type
        bigint supplier_id FK "UK with vendor_bill_number"
        bigint employee_id FK
        smallint company_gst_registration_id FK
        smallint warehouse_id FK
        bigint approved_by_employee_id FK
        varchar expense_status
    }
    expense_line_items {
        bigint expense_line_item_id PK
        bigint expense_id FK
        smallint expense_category_id FK
        smallint cost_center_id FK
        bigint marketing_campaign_id FK
    }
    expense_payments {
        bigint expense_payment_id PK
        varchar payment_number UK
        bigint expense_id FK
        smallint bank_account_id FK
        smallint payment_method_id FK
        varchar payment_status
    }
    expense_attachments_metadata {
        bigint expense_attachment_id PK
        bigint expense_id FK
        char sha256_checksum UK
        bigint uploaded_by_employee_id FK
    }

    suppliers |o--o{ expenses : "vendor bill"
    employees |o--o{ expenses : "claimant"
    company_gst_registrations |o--o{ expenses : "billed to"
    warehouses |o--o{ expenses : "site / petty cash"
    expenses ||--|{ expense_line_items : "contains"
    expense_categories ||--o{ expense_line_items : "classified as"
    cost_centers ||--o{ expense_line_items : "charged to"
    marketing_campaigns |o--o{ expense_line_items : "campaign spend"
    expenses ||--o{ expense_payments : "settled by"
    bank_accounts ||--o{ expense_payments : "paid from"
    payment_methods ||--o{ expense_payments : "method"
    expenses ||--o{ expense_attachments_metadata : "supported by"
```

---

## 12. Journals and Ledger

```mermaid
erDiagram
    journal_entries {
        bigint journal_entry_id PK
        varchar journal_number UK
        varchar journal_source
        varchar journal_type
        date posting_date
        smallint accounting_period_id FK
        varchar posting_key UK "generated"
        bigint reverses_journal_entry_id FK, UK
        bigint generation_run_id FK
        decimal total_debit
        decimal total_credit
    }
    journal_entry_lines {
        bigint journal_entry_line_id PK
        bigint journal_entry_id FK "UK with line_number"
        int account_id FK
        decimal debit_amount
        decimal credit_amount
        smallint cost_center_id FK
        bigint customer_id FK
        bigint supplier_id FK
        bigint employee_id FK
        smallint warehouse_id FK
    }
    gl_period_balances {
        bigint gl_period_balance_id PK
        int account_id FK "UK with accounting_period_id"
        smallint accounting_period_id FK
        decimal opening_balance
        decimal closing_balance
    }

    accounting_periods ||--o{ journal_entries : "posted in (OPEN)"
    journal_entries |o--o| journal_entries : "reverses"
    generation_runs ||--o{ journal_entries : "written by"
    journal_entries ||--|{ journal_entry_lines : "debits and credits"
    chart_of_accounts ||--o{ journal_entry_lines : "posted to"
    cost_centers |o--o{ journal_entry_lines : "P&L analysis"
    customers |o--o{ journal_entry_lines : "AR party"
    suppliers |o--o{ journal_entry_lines : "AP party"
    employees |o--o{ journal_entry_lines : "AP party (claims)"
    warehouses |o--o{ journal_entry_lines : "inventory by warehouse"
    chart_of_accounts ||--o{ gl_period_balances : "summarised"
    accounting_periods ||--o{ gl_period_balances : "per period"
```

`v_general_ledger` (view) = `journal_entry_lines` ⋈ `journal_entries` ⋈ `accounting_periods` ⋈ `chart_of_accounts` ⋈ `account_types`, with a running balance per account (1E §3).

### 12.1 Journal sources (one journal per event, D50)

Each journal has **at most one** of these 16 source FKs; OPENING, RECURRING and MANUAL journals have none. Posting rules are in 1E §6.

```mermaid
erDiagram
    goods_receipts |o--o{ journal_entries : "GOODS_RECEIPT"
    supplier_invoices |o--o{ journal_entries : "SUPPLIER_INVOICE"
    supplier_payments |o--o{ journal_entries : "SUPPLIER_PAYMENT"
    invoices |o--o{ journal_entries : "SALES_INVOICE incl. COGS"
    credit_notes |o--o{ journal_entries : "CREDIT_NOTE"
    customer_payments |o--o{ journal_entries : "CUSTOMER_PAYMENT"
    refunds |o--o{ journal_entries : "REFUND"
    sales_returns |o--o{ journal_entries : "SALES_RETURN_RECEIPT"
    shipments |o--o{ journal_entries : "RTO_RECEIPT"
    stock_adjustments |o--o{ journal_entries : "STOCK_ADJUSTMENT"
    stock_transfers |o--o{ journal_entries : "TRANSFER_DISPATCH / RECEIPT"
    defective_stock |o--o{ journal_entries : "DEFECTIVE_DISPOSITION"
    expenses |o--o{ journal_entries : "EXPENSE"
    expense_payments |o--o{ journal_entries : "EXPENSE_PAYMENT"
    bank_transactions |o--o{ journal_entries : "BANK_SETTLEMENT / CHARGES"
    gst_returns |o--o{ journal_entries : "GST_SETTLEMENT"
```

---

## 13. Bank

```mermaid
erDiagram
    bank_transactions {
        bigint bank_transaction_id PK
        smallint bank_account_id FK
        date statement_date
        varchar transaction_type
        decimal credit_amount "money in"
        decimal debit_amount "money out"
        bigint customer_payment_id FK "at most one document"
        bigint supplier_payment_id FK
        bigint expense_payment_id FK
        bigint refund_id FK
        bigint gst_return_id FK
        bigint counter_bank_transaction_id FK, UK
        smallint shipping_carrier_id FK
        bigint bank_reconciliation_id FK
    }
    bank_reconciliations {
        bigint bank_reconciliation_id PK
        smallint bank_account_id FK "UK with accounting_period_id"
        smallint accounting_period_id FK
        decimal difference_amount
        varchar reconciliation_status
    }

    bank_accounts ||--o{ bank_transactions : "statement of"
    customer_payments |o--o| bank_transactions : "direct receipt"
    supplier_payments |o--o| bank_transactions : "payment"
    expense_payments |o--o| bank_transactions : "payment"
    refunds |o--o| bank_transactions : "refund"
    gst_returns |o--o| bank_transactions : "GST challan"
    bank_transactions |o--o| bank_transactions : "internal transfer leg"
    shipping_carriers |o--o{ bank_transactions : "COD remittance"
    bank_reconciliations |o--o{ bank_transactions : "reconciles"
    bank_accounts ||--o{ bank_reconciliations : "reconciled"
    accounting_periods ||--o{ bank_reconciliations : "for period"
```

---

## 14. Receivables, Payables & Tax

```mermaid
erDiagram
    accounts_receivable {
        bigint accounts_receivable_id PK
        bigint customer_id FK
        varchar item_type
        bigint invoice_id FK, UK "one of"
        bigint credit_note_id FK, UK "one of"
        bigint customer_payment_id FK, UK "one of"
        bigint refund_id FK, UK "one of"
        decimal open_amount "signed"
        bigint journal_entry_id FK
    }
    customer_payment_allocations {
        bigint customer_payment_allocation_id PK
        bigint customer_payment_id FK "credit side, one of"
        bigint credit_note_id FK "credit side, one of"
        bigint invoice_id FK "debit side, one of"
        bigint refund_id FK "debit side, one of"
        decimal allocated_amount
    }
    accounts_payable {
        bigint accounts_payable_id PK
        bigint supplier_id FK "payee, one of"
        bigint employee_id FK "payee, one of"
        varchar item_type
        bigint supplier_invoice_id FK, UK "one of"
        bigint expense_id FK, UK "one of"
        bigint supplier_payment_id FK, UK "one of"
        decimal open_amount "signed"
        bigint journal_entry_id FK
    }
    supplier_payment_allocations {
        bigint supplier_payment_allocation_id PK
        bigint supplier_payment_id FK "UK with supplier_invoice_id"
        bigint supplier_invoice_id FK
        decimal allocated_amount
    }
    tax_transactions {
        bigint tax_transaction_id PK
        smallint company_gst_registration_id FK
        varchar tax_direction
        bigint invoice_line_item_id FK "one of"
        bigint credit_note_line_item_id FK "one of"
        bigint supplier_invoice_line_item_id FK "one of"
        bigint expense_line_item_id FK "one of"
        bigint stock_transfer_line_item_id FK "one of"
        bigint customer_id FK
        bigint supplier_id FK
        smallint place_of_supply_state_id FK
        char return_period "generated"
        bigint gst_return_id FK
    }
    gst_returns {
        bigint gst_return_id PK
        smallint company_gst_registration_id FK "UK with type, period"
        varchar return_type
        char return_period
        smallint bank_account_id FK
        varchar arn UK
    }
    gst_return_line_items {
        bigint gst_return_line_item_id PK
        bigint gst_return_id FK "UK with line_number"
        varchar section_code
        smallint place_of_supply_state_id FK
    }

    customers ||--o{ accounts_receivable : "owes"
    invoices |o--o| accounts_receivable : "open item"
    credit_notes |o--o| accounts_receivable : "open item"
    customer_payments |o--o| accounts_receivable : "open item"
    refunds |o--o| accounts_receivable : "open item"
    journal_entries ||--o| accounts_receivable : "posted by"
    customer_payments |o--o{ customer_payment_allocations : "applied"
    credit_notes |o--o{ customer_payment_allocations : "applied"
    invoices |o--o{ customer_payment_allocations : "settled"
    refunds |o--o{ customer_payment_allocations : "settled"
    suppliers |o--o{ accounts_payable : "owed"
    employees |o--o{ accounts_payable : "owed (claims)"
    supplier_invoices |o--o| accounts_payable : "open item"
    expenses |o--o| accounts_payable : "open item"
    supplier_payments |o--o| accounts_payable : "open item"
    journal_entries ||--o| accounts_payable : "posted by"
    supplier_payments ||--o{ supplier_payment_allocations : "applied"
    supplier_invoices ||--o{ supplier_payment_allocations : "settled"
    company_gst_registrations ||--o{ tax_transactions : "our GSTIN"
    invoice_line_items |o--o| tax_transactions : "output"
    credit_note_line_items |o--o| tax_transactions : "output (negative)"
    supplier_invoice_line_items |o--o| tax_transactions : "input"
    expense_line_items |o--o| tax_transactions : "input"
    stock_transfer_line_items |o--o{ tax_transactions : "output + input"
    customers |o--o{ tax_transactions : "buyer"
    suppliers |o--o{ tax_transactions : "seller"
    states ||--o{ tax_transactions : "place of supply"
    gst_returns |o--o{ tax_transactions : "reported in"
    company_gst_registrations ||--o{ gst_returns : "files"
    bank_accounts |o--o{ gst_returns : "paid from"
    gst_returns ||--|{ gst_return_line_items : "sections"
    states |o--o{ gst_return_line_items : "by state"
```

---

## 15. System and Generation Control (keys only; columns in 1G)

```mermaid
erDiagram
    document_sequences {
        int document_sequence_id PK
        varchar document_type "UK with GSTIN, fiscal year"
        smallint company_gst_registration_id FK
        smallint fiscal_year
        int last_sequence
    }
    generation_runs {
        bigint generation_run_id PK
        int run_number UK
        varchar run_mode
        varchar run_status
    }
    generation_run_steps {
        bigint generation_run_step_id PK
        bigint generation_run_id FK
        varchar step_name
    }

    company_gst_registrations |o--o{ document_sequences : "numbering series"
    generation_runs ||--|{ generation_run_steps : "progress"
    generation_runs ||--o{ inventory_transactions : "wrote"
    generation_runs ||--o{ journal_entries : "wrote"
```

---

## 16. End-to-End Traceability (master prompt §15A)

The two required chains as a path through the diagrams. Every arrow is a real FK, walkable in both directions.

```mermaid
flowchart LR
    subgraph Procurement
        A1[rfq_headers] --> A2[rfq_suppliers] --> A3[quotation_headers] --> A4[purchase_order_headers]
        A4 --> A5[goods_receipts] --> A6[goods_inspections]
        A6 --> A7[inventory_transactions]
        A4 --> A8[supplier_invoices] --> A9[accounts_payable] --> A10[supplier_payment_allocations] --> A11[supplier_payments]
        A8 --> AJ[journal_entries]
        A11 --> AJ
    end
    subgraph Sales
        B1[orders] --> B2[order_line_items] --> B3[inventory_reservations]
        B2 --> B4[shipment_line_items] --> B5[inventory_transactions]
        B4 --> B6[invoice_line_items] --> B7[accounts_receivable] --> B8[customer_payment_allocations] --> B9[customer_payments]
        B4 --> B10[delivery_attempts]
        B6 --> B11[sales_return_line_items] --> B12[return_inspections] --> B13[inventory_transactions]
        B11 --> B14[credit_note_line_items] --> B15[refunds]
        B6 --> BJ[journal_entries]
        B9 --> BJ
        B15 --> BJ
    end
```

---

## 17. Diagram Validation

All **16** Mermaid diagrams in this document were parsed with the Mermaid library (v11.4.1, under Node.js with jsdom) on 2026-11-08: 16 parsed, 0 failed. To show the check really detects errors, two deliberately broken diagrams were run through the same script and both were rejected. Repeat the check whenever a diagram changes. A reusable `validate-docs` command can be added in Phase 6 (documentation).

---

## 18. What Comes Next

Part 1G: historical and continuous generation architecture, workspaces and re-runs (A2), the system tables (`document_sequences`, `generation_runs`, `generation_run_steps`), the DuckDB loader (full / incremental / watermarks), CDC and anomaly simulation architecture. Resolves Q4 and Q5.
