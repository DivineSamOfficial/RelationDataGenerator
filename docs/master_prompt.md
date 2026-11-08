# Master Prompt — Project Requirements

> Original requirements from the project owner. Decisions recorded in `docs/design/` refine these, e.g. merged/dropped tables (R1–R22), native MySQL instead of Docker (D8), workspaces per company (D13), the added Control Panel UI as Phase 5, and phases 6–7 renumbered.

## 1. Project Overview

I want to build a Python-based Synthetic Data Generation Platform that simulates the operational database (OLTP system) of a realistic, fictional supply chain and trading company.

The purpose of this project is to create a fully interconnected relational database containing realistic business transactions, operational records, procurement activities, inventory movements, financial transactions, and accounting information.

The generated data will be used to practise:

- SQL and advanced SQL querying.
- Python-based ETL pipelines.
- Batch and incremental data ingestion.
- CDC (Change Data Capture) concepts.
- Data warehousing and dimensional modelling.
- Star and Snowflake schema development.
- dbt transformations.
- Apache Airflow orchestration.
- DuckDB analytical workloads.
- Data quality testing and reconciliation.
- Business intelligence and financial reporting.

The database must behave like a real company's operational system rather than a collection of independent Faker-generated tables.

The objective is to build a realistic source system that can serve as the foundation for multiple data engineering and analytics engineering projects.

## 2. Technology Stack

Use the following technologies:

| Component | Technology |
|---|---|
| Programming language | Python |
| Synthetic data generation | Faker |
| OLTP database | MySQL 8.x |
| Database interaction | SQLAlchemy / PyMySQL |
| Schema management | SQL DDL / SQLAlchemy |
| Analytical database | DuckDB |
| Data loading | Python-based configurable loader |
| Configuration | YAML / environment variables |
| Logging | Python logging |
| CLI | Typer or argparse |

Use modular Python architecture. Avoid putting the entire application into one Python script.

## 3. Company Context and Business Domain

Create one fictional company operating as a supply chain, distribution and trading business.

The company should:

- Purchase products from multiple suppliers.
- Maintain warehouses and inventory.
- Sell products to multiple customers.
- Process customer orders.
- Generate invoices and collect payments.
- Manage shipments and deliveries.
- Handle customer returns, refunds and defective products.
- Manage procurement through RFQs, quotations and purchase orders.
- Record business expenses.
- Maintain financial accounting records.

Use India as the default business environment, with INR as the base currency and GST-based taxation.

Make the country, currency, taxation configuration and financial year configurable.

Use realistic product names, supplier names, customer information, locations, addresses, GSTIN-like synthetic identifiers, and transaction dates.

All generated personal and business information must be fictional. Never use real personal information or actual GSTINs.

## 4. Database Modules and Required Tables

Design the database using properly normalized relational tables, appropriate primary keys, foreign keys, unique constraints and indexes.

### A. Master Data

Create:

- companies
- business_units
- warehouses
- warehouse_locations
- customers
- customer_addresses
- suppliers
- supplier_addresses
- products
- product_categories
- product_brands
- product_units_of_measure
- product_tax_rates
- payment_methods
- shipping_carriers
- employees
- expense_categories

Master data must be generated before transactional data.

### B. Sales and Order Management

Create:

- orders (order header)
- order_line_items (order details)
- order_status_history
- customer_payments
- payment_allocations

Requirements:

- One order can contain multiple products.
- Each order line must reference an existing product.
- Each order must reference an existing customer.
- Orders should have realistic order dates, statuses, quantities, discounts, taxes and totals.
- Payment records must support full payments, partial payments and multiple payment attempts.
- Maintain historical order status changes.

### C. Product and Inventory Management

Create:

- inventory
- inventory_transactions
- stock_adjustments
- stock_transfers
- stock_transfer_line_items
- inventory_reservations
- inventory_batches
- stock_movements

Requirements:

- Inventory must be maintained at warehouse and product level.
- Purchases increase available stock.
- Sales reduce stock or reserve stock.
- Returns can increase sellable or defective stock.
- Stock transfers must affect both source and destination warehouses.
- Inventory quantities must reconcile with the underlying stock movements.
- Support reorder levels and low-stock scenarios.

### D. Invoicing

Create:

- invoices (invoice header)
- invoice_line_items
- invoice_payments
- credit_notes
- credit_note_line_items

Invoices must be associated with customer orders.

Support:

- Multiple invoice line items.
- GST calculations.
- Discounts.
- Partial invoicing where appropriate.
- Invoice cancellation.
- Credit notes for returns and adjustments.
- Outstanding invoice balances.

### E. Returns, Refunds and Defective Inventory

Create:

- sales_returns (return header)
- sales_return_line_items
- return_inspections
- refunds
- refund_line_items
- returned_goods
- defective_stock
- defective_stock_transactions

Business rules:

- A return must reference the original order and relevant order line.
- Returned quantities cannot exceed eligible sold quantities.
- Return inspections determine whether goods are sellable, defective or require further inspection.
- Refunds must reference the relevant customer payment or original invoice.
- Returned goods must be traceable to their original product and transaction.
- Defective stock must have a reason, inspection result and stock movement history.
- Support partial returns and partial refunds.

### F. Shipping and Logistics

Create:

- shipments
- shipment_line_items
- shipment_tracking
- shipment_status_history
- delivery_attempts

Include realistic shipping statuses:

- Pending
- Order Confirmed
- Picking
- Picked
- Packing
- Packed
- Ready for Pickup
- Picked Up
- In Transit
- Out for Delivery
- Delivered
- Delivery Failed
- Returned to Sender
- Cancelled

Each shipment should be associated with an order. Support split shipments where one order is fulfilled through multiple shipments.

Maintain timestamps and tracking references for status changes.

### G. Procurement and Supplier Management

This module is particularly important. Use proper header and line-item structures.

**RFQ (Request for Quotation)**

- rfq_headers
- rfq_line_items
- rfq_suppliers
- rfq_status_history

**Supplier Quotations**

- quotation_headers
- quotation_line_items
- quotation_status_history

**Purchase Orders**

- purchase_order_headers
- purchase_order_line_items
- purchase_order_status_history

**Goods Receipt**

- goods_receipts
- goods_receipt_line_items
- goods_inspections

Business rules:

- An RFQ can contain multiple products.
- An RFQ can be sent to multiple suppliers.
- Multiple suppliers can submit quotations against an RFQ.
- A supplier quotation can contain multiple line items.
- Purchase orders should reference the accepted supplier quotation where applicable.
- A purchase order can contain multiple products.
- Goods receipts must reference the relevant purchase order and line items.
- Partial deliveries and partial receipts must be supported.
- Receiving goods must update inventory.
- Rejected or damaged goods must be recorded separately.

Maintain the complete procurement lifecycle:

RFQ → Supplier Quotation → Quotation Evaluation → Purchase Order → Goods Receipt → Inventory Update → Supplier Invoice → Supplier Payment.

### H. Business Expenses

Create:

- expenses
- expense_line_items
- expense_categories
- expense_payments
- expense_attachments_metadata

Generate realistic business expenses, including:

- Office rent.
- Electricity and internet.
- Employee-related expenses.
- Travel and transportation.
- Office purchases and stationery.
- Software subscriptions.
- AI subscriptions.
- Cloud hosting.
- Marketing and advertising.
- Warehouse expenses.
- Logistics expenses.
- Professional services.
- GST payments.
- Petty cash transactions.
- Maintenance and repairs.

Include expense dates, amounts, GST components, payment methods, vendors, approval statuses and supporting reference numbers.

Support recurring monthly expenses and one-time expenses.

### I. Financial Accounting

Build a basic double-entry accounting subsystem.

Create the following tables:

**Accounting Master Data**

- chart_of_accounts
- account_types
- accounting_periods
- cost_centers
- tax_accounts

**Accounting Transactions**

- journal_entries (journal header)
- journal_entry_lines (debit and credit lines)
- general_ledger (account-level ledger representation or derived ledger)
- bank_transactions
- bank_reconciliations

**Accounts Receivable**

- accounts_receivable
- customer_payment_allocations

**Accounts Payable**

- accounts_payable
- supplier_payment_allocations

**Tax Management**

- tax_transactions
- gst_returns
- gst_return_line_items

Accounting requirements:

- Use double-entry bookkeeping.
- Every posted journal entry must have equal total debits and credits.
- Sales invoices must generate accounting entries.
- Customer receipts must reduce accounts receivable.
- Purchase invoices must generate accounts payable entries.
- Supplier payments must reduce accounts payable.
- Business expenses must generate appropriate accounting entries.
- Inventory purchases and sales must have corresponding inventory and cost-of-goods-sold accounting treatment.
- Returns, refunds, credit notes and GST adjustments must have appropriate accounting entries.
- Support journal reversals and adjustments.
- Support fiscal periods and financial year reporting.

Generate a realistic chart of accounts covering assets, liabilities, equity, revenue, cost of goods sold and operating expenses.

The accounting subsystem must be relationally connected to the operational transactions. Avoid generating accounting records independently of the business transactions.

The journal entries and journal entry lines should be the accounting source of truth. General ledger records must be clearly defined as a derived or maintained ledger representation, avoiding competing accounting sources of truth.

For example, a customer invoice should generate appropriate receivable, revenue and GST entries.

Ensure:

Total Debits = Total Credits

for every posted journal entry.

### J. Additional Accounting and Financial Reporting Requirements

Ensure the database can support the generation of:

- Trial Balance.
- Profit and Loss Statement.
- Balance Sheet.
- Cash Flow Statement.
- Accounts Receivable Ageing.
- Accounts Payable Ageing.
- General Ledger Report.
- Customer Outstanding Report.
- Supplier Outstanding Report.
- GST Input Tax Report.
- GST Output Tax Report.
- Inventory Valuation Report.
- Cost of Goods Sold Report.

These reports do not necessarily need to be generated by the OLTP application itself. The relational database should contain sufficient information to build them through downstream SQL and data modelling.

## 5. Data Generation and Business Logic

This is the most important part of the project.

Do not generate every table independently using random Faker values.

Implement a business-aware data generation engine.

Examples:

1. Generate master data first.
2. Generate supplier RFQs.
3. Generate supplier quotations against RFQs.
4. Generate purchase orders based on accepted quotations.
5. Generate goods receipts against purchase orders.
6. Update inventory based on received quantities.
7. Generate customer orders using available products.
8. Generate order line items.
9. Generate invoices and payments.
10. Generate shipments and tracking events.
11. Generate customer returns against existing orders.
12. Generate refunds and returned/defective stock movements.
13. Generate business expenses.
14. Generate corresponding accounting journal entries.

All foreign key relationships must be valid.

All business rules must be enforced.

Maintain realistic timestamps and transaction sequences.

Ensure that operational transactions and accounting entries are generated as consequences of business events.

## 6. Synthetic Data Scenarios

Include different business scenarios rather than only successful transactions.

Examples:

- Orders with successful deliveries.
- Orders with partial deliveries.
- Cancelled orders.
- Failed payment attempts.
- Partial payments.
- Overdue invoices.
- Partial customer returns.
- Full customer returns.
- Refunds pending approval.
- Refunds completed.
- Returned products passing inspection.
- Defective goods.
- Supplier quotations rejected.
- Purchase orders partially received.
- Purchase orders with rejected goods.
- Delayed supplier deliveries.
- Stock shortages.
- Inventory adjustments.
- Supplier payment delays.
- Recurring and irregular business expenses.

Ensure that generated scenarios are internally consistent.

Use configurable scenario distributions so the generated dataset does not contain unrealistic proportions of any one business outcome.

## 7. Data Volume and Generation Configuration

Provide configurable generation volumes.

For example:

- Customers: 1,000
- Suppliers: 100
- Products: 500
- Warehouses: 5
- Orders: 50,000
- Order line items: dynamically generated
- Invoices: dynamically generated
- Purchase orders: 5,000
- RFQs: 2,000
- Quotations: dynamically generated
- Inventory transactions: dynamically generated
- Accounting journal entries: dynamically generated

These should be configurable through a YAML configuration file or CLI arguments.

Allow users to specify:

- Number of records.
- Start date and end date.
- Historical data duration.
- Random seed for reproducibility.
- Number of warehouses.
- Number of customers and suppliers.
- Transaction distribution.
- Scenario percentages.
- Batch size.

Support both initial bulk generation and subsequent incremental data generation.

## 8. MySQL Database Implementation

Create a dedicated MySQL database.

Requirements:

- Generate all required CREATE TABLE statements.
- Define appropriate data types.
- Define primary and foreign keys.
- Create indexes for frequently queried columns.
- Add appropriate unique constraints.
- Use DECIMAL for financial amounts.
- Use DATE and DATETIME/TIMESTAMP appropriately.
- Use transactional inserts where required.
- Implement batch insertion for large datasets.
- Avoid inefficient row-by-row insertion.
- Ensure referential integrity.

Provide scripts to:

1. Create the database.
2. Create the schema.
3. Generate synthetic data.
4. Load generated data into MySQL.
5. Reset the database when required.

## 9. DuckDB Data Loader

Build a separate Python-based data loader that extracts data from MySQL and loads it into DuckDB.

The loader must support:

**Initial Load**

- Create a DuckDB database.
- Create corresponding tables.
- Extract all records from MySQL.
- Load records into DuckDB.
- Preserve source column names and data types as closely as possible.

**Incremental Load**

- Identify newly inserted or updated records.
- Use updated_at, created_at or another suitable watermark where appropriate.
- Support configurable batch sizes.
- Avoid duplicate records.
- Handle inserts and updates.
- Maintain load metadata.

**Metadata Tables**

Create:

- etl_load_history
- etl_table_watermarks
- etl_error_log

Track:

- Source table.
- Target table.
- Load type.
- Start and end timestamps.
- Number of extracted records.
- Number of inserted records.
- Number of updated records.
- Number of failed records.
- Load status.

The loader should be reusable for every table without writing a completely separate extraction function for each table.

It should also support full reloads and table-level reloads.

Design incremental loading so that it correctly handles updates occurring during extraction, watermark boundaries, retries and failed batches.

For tables without reliable update timestamps, document and implement an appropriate alternative, such as CDC simulation or full reload.

## 10. Data Quality and Validation

Implement automated checks for:

- Orphan records.
- Duplicate primary keys.
- Invalid foreign keys.
- Invalid transaction dates.
- Negative or impossible quantities.
- Incorrect invoice totals.
- Incorrect tax calculations.
- Inventory reconciliation.
- Return quantity validation.
- Purchase order versus goods receipt reconciliation.
- Accounts receivable reconciliation.
- Accounts payable reconciliation.
- Debit-credit balancing.
- MySQL versus DuckDB row-count reconciliation.

Provide a validation report after generation and loading.

## 11. Project Structure

Organize the application into modular components. A suggested structure:

synthetic_supply_chain/

- config/
  - settings.yaml
  - database.yaml
- database/
  - mysql_connection.py
  - schema/
  - migrations/
- generators/
  - master_data/
  - sales/
  - inventory/
  - procurement/
  - shipping/
  - returns/
  - expenses/
  - accounting/
- business_logic/
- loaders/
  - mysql_loader.py
  - duckdb_loader.py
  - incremental_loader.py
- validation/
- orchestration/
- cli/
- logs/
- tests/
- main.py
- requirements.txt
- README.md

Refine this structure where necessary.

## 12. CLI Requirements

Provide a command-line interface for controlling the application.

Example commands:

- Initialize the MySQL database.
- Generate master data.
- Generate all transactional data.
- Generate only sales data.
- Generate only procurement data.
- Generate only accounting data.
- Generate additional transactions.
- Load all MySQL tables into DuckDB.
- Incrementally load changed records.
- Reload a specific table.
- Validate generated data.
- Reset the environment.

The CLI should expose configuration options without requiring users to modify the source code.

## 13. Development Approach and Deliverables

Do not attempt to implement the entire system in one step.

Follow this development sequence:

### Phase 1 – Architecture and Database Design

- Define the fictional company's business processes.
- Identify all entities.
- Design the complete relational schema.
- Define primary keys and foreign keys.
- Define cardinality and table relationships.
- Identify transaction dependencies.
- Produce an ERD.
- Explain the accounting flow and double-entry logic.
- Identify any missing tables or relationships.
- Define the historical generation and anomaly simulation architecture.

### Phase 2 – Database Implementation

- Create MySQL database.
- Implement schema.
- Add constraints and indexes.
- Test schema creation.

### Phase 3 – Data Generation Engine

- Implement master data generation.
- Implement transactional data generation.
- Implement business rules.
- Implement realistic scenarios.
- Validate relationships and data consistency.
- Implement historical and continuous generation.

### Phase 4 – DuckDB Loader

- Implement initial loading.
- Implement incremental loading.
- Implement metadata and audit tables.
- Implement reconciliation.

### Phase 5 – Testing and Documentation

- Create automated tests.
- Validate business rules.
- Document database architecture.
- Document table relationships.
- Provide setup instructions.
- Provide usage examples.

### Phase 6 – Advanced Data Engineering Simulation

- Implement controlled source-system changes.
- Implement CDC simulation.
- Implement late-arriving records.
- Implement schema evolution scenarios.
- Implement configurable anomaly injection.
- Implement failed-load and recovery scenarios.
- Validate data quality and reconciliation after anomaly remediation.

## 14. Important Development Instructions

- Prioritize correctness of the relational database design over generating large volumes of data.
- Never create independent random records when a business relationship should exist.
- Avoid unnecessary denormalization in the OLTP database.
- Use surrogate primary keys where appropriate, while retaining business reference numbers.
- Make data generation deterministic when a seed is provided.
- Ensure the system can generate additional transactions without breaking existing relationships.
- Make the application restartable and idempotent where appropriate.
- Use proper error handling and logging.
- Write maintainable, reusable and modular Python.
- Do not hardcode database credentials.
- Keep MySQL and DuckDB responsibilities separate.
- Do not use DuckDB as the OLTP source database.
- Explain important architectural decisions instead of making assumptions silently.
- Ensure generated transactions have appropriate chronological and financial consistency.
- Avoid creating unnecessary tables merely to increase the schema size. Every table must have a clear business purpose.
- Identify and resolve redundant entities, duplicated responsibilities and ambiguous relationships during Phase 1.

### Critical Data Integrity Requirement

The default OLTP data generation mode must ALWAYS generate clean, realistic and business-consistent data.

Intentionally incorrect or anomalous data must NEVER be mixed into the default generation process.

Implement a separate, explicitly activated **Anomaly Simulation Mode**.

This distinction is mandatory.

The clean generation mode must:

- Maintain valid primary and foreign key relationships.
- Maintain accurate financial calculations.
- Maintain inventory consistency.
- Maintain correct order, invoice and payment relationships.
- Maintain balanced accounting journal entries.
- Pass all applicable business validation checks.

The separate Anomaly Simulation Mode should allow controlled introduction of data engineering challenges, including:

- Duplicate source events.
- Late-arriving records.
- Missing optional attributes.
- Unexpected NULL values.
- Incorrect source timestamps.
- Out-of-order status events.
- Schema changes such as newly introduced columns.
- Failed batch loads.
- Interrupted ingestion.
- Records requiring correction or reprocessing.

Anomaly simulation must be configurable by anomaly type, target table, record count or percentage, and execution scenario.

Where possible, keep anomaly injection isolated from the clean OLTP source database. Use a separate simulation layer or controlled staging mechanism so that clean operational records remain recoverable.

The platform must support practising both:

1. Reliable ETL/ELT pipelines using clean, business-consistent source data.
2. Data quality remediation, error handling, CDC and recovery using intentionally imperfect source data.

## 15. Advanced Realism, Historical Data and Data Engineering Scenarios

### A. End-to-End Transaction Traceability

Every business transaction must maintain traceability across the entire operational lifecycle.

Examples:

**Procurement:**

RFQ → Supplier Quotation → Purchase Order → Goods Receipt → Inventory Transaction → Supplier Invoice → Accounts Payable → Supplier Payment → Journal Entry.

**Sales:**

Customer Order → Order Line Items → Invoice → Customer Payment → Shipment → Delivery → Customer Return → Refund → Inventory Movement → Journal Entry.

Ensure that the relationships allow a transaction to be traced backwards and forwards across the database.

### B. Historical Data and Continuous Generation

Support two distinct generation modes.

**Initial Historical Generation**

- Generate 1–3 years of historical business data.
- Generate transactions across the configured date range.
- Ensure realistic chronological dependencies.
- Include both completed and outstanding transactions.
- Distribute transactions realistically across weekdays, weekends, months and financial periods.

**Continuous Transaction Generation**

- Allow additional business transactions to be generated without resetting the database.
- Generate new orders, procurement transactions, payments, expenses, shipments and other business events.
- Allow existing transactions to change status over time.
- Maintain historical status changes and updated timestamps.
- Ensure newly generated transactions reference existing master and transactional records appropriately.
- Avoid duplicate business transactions when generation is restarted.

### C. Source Change Simulation and CDC Practice

Introduce controlled source-system changes to simulate real OLTP behaviour.

Include:

- New record insertions.
- Updates to existing records.
- Status changes.
- Changes to customer and supplier information.
- Inventory adjustments.
- Corrections to transactional records.
- Soft deletions where appropriate.

Maintain created_at, updated_at and deleted_at fields where applicable.

Consider implementing an optional change event log to simulate CDC events, including INSERT, UPDATE and DELETE operations.

The system should support practising incremental ingestion, watermark-based extraction, CDC processing and late-arriving records.

Clearly distinguish between actual business events and simulated source-system change events.

### D. Controlled Data Anomalies

Introduce configurable data engineering challenges without corrupting the core business transactions by default.

Examples:

- Duplicate source events.
- Late-arriving records.
- Missing optional attributes.
- Unexpected NULL values.
- Incorrect source timestamps.
- Out-of-order status events.
- Schema changes such as new columns.
- Failed batch loads.
- Interrupted ingestion.
- Records requiring correction or reprocessing.

Provide separate anomaly simulation controls so that clean data generation and intentionally imperfect source data can be tested independently.

Anomalies should be documented and identifiable so that users can test detection, quarantine, remediation and reprocessing.

### E. Data Reconciliation and Consistency

Implement cross-module reconciliation checks.

Examples:

- Purchase order quantities versus received quantities.
- Goods receipts versus inventory movements.
- Orders versus invoices.
- Invoices versus customer payments.
- Customer returns versus refunds.
- Returned goods versus sellable and defective inventory.
- Supplier invoices versus accounts payable.
- Payments versus outstanding balances.
- Accounting journal entries versus operational transactions.
- Inventory balances versus historical stock movements.

Ensure that the clean generation mode always passes the relevant business consistency checks.

For anomaly simulation, generate expected validation failures and provide a way to identify which checks should fail.

### F. Reproducibility and Scenario Configuration

Allow users to configure:

- Historical data duration.
- Transaction generation frequency.
- Business scenario distribution.
- Anomaly injection percentage.
- Source change frequency.
- Batch sizes.
- Random seed.
- Data generation date.
- Anomaly types and affected tables.

The same configuration and random seed should produce reproducible initial datasets.

Keep anomaly injection optional and configurable.

## 16. Expected Final Outcome

The final application should provide:

1. A fully functional MySQL OLTP database.
2. A realistic, interconnected supply chain business schema.
3. A modular Python Faker-based synthetic data generator.
4. A configurable historical and continuous transaction generator.
5. A complete accounting and financial transaction subsystem.
6. A reusable MySQL-to-DuckDB data loader.
7. Full and incremental loading capabilities.
8. Data quality and reconciliation validation.
9. A separate controlled anomaly simulation framework.
10. Documentation and an ERD explaining the entire database.

The application should be suitable for generating datasets ranging from thousands to millions of records, subject to database and hardware capacity.

---

## 17. Instructions for Starting the Project

**Most importantly: Begin with Phase 1 only.**

Before writing any Python implementation code, produce the complete database architecture, including:

1. Fictional company business-process overview.
2. Complete list of proposed entities and tables.
3. Detailed table definitions and column descriptions.
4. Primary key and foreign key relationships.
5. Relationship cardinalities (1:1, 1:N, M:N).
6. Complete ERD, preferably divided into logical modules.
7. Procurement transaction flow.
8. Sales and order transaction flow.
9. Inventory movement flow.
10. Returns and refunds flow.
11. Financial accounting flow.
12. Historical data and continuous generation architecture.
13. DuckDB loading architecture.
14. CDC and anomaly simulation architecture.
15. Identification of potential redundant tables or missing entities.

For the accounting subsystem, explain how operational transactions translate into double-entry journal entries.

For the data loader, explain how full loads, incremental loads, watermarks and CDC simulation will work.

For anomaly simulation, explain how clean source data will be protected and how intentionally imperfect data will be introduced separately.

Review the entire design for missing relationships, redundant entities, inconsistent business rules and architectural issues.

Do not begin implementation until the complete Phase 1 design has been presented and approved by me.

After presenting Phase 1, wait for my approval before proceeding to Phase 2.