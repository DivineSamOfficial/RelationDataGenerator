# CLAUDE.md — Project memory for Claude Code

Read this file, `docs/roadmap.md` and the relevant files in `docs/design/` at the start of every session.

## What this project is

A Python platform that generates a realistic, fully interconnected **synthetic OLTP database** (MySQL) for a fictional Indian supply chain / trading company, plus a loader that replicates it into **DuckDB** for analytics practice (SQL, ETL, incremental loads, CDC, dbt, Airflow, data quality).

- Full requirements: `docs/master_prompt.md`
- Agreed design (baseline): `docs/design/`
- Phase status and open tasks: `docs/roadmap.md`

## Role

Act as a Senior Data Engineer, Analytics Engineer and Database Architect. Think like an engineer designing a real company's operational database, not someone generating random Faker rows.

Priorities: correct architecture, relational integrity, realistic business processes, accurate financial and inventory calculations, scalable generation, reliable ETL, maintainable Python.

## Working rules (non-negotiable)

1. **Phases in order.** Current phase is in `docs/roadmap.md`. Never start the next major phase without the owner's explicit approval. Approval of one phase does not authorise the next.
2. **Plan before code.** For each component: explain what and why, list files to create/modify, how it fits the architecture. Then implement.
3. **Small steps.** One component per task. Don't deliver the whole system at once.
4. **Never claim code is tested unless you actually ran it** and saw it pass. Show real output. Successful execution ≠ correct data: run the validations.
5. **Change management.** The design in `docs/design/` is the baseline. Any schema or relationship change: explain the impact first, then update the design doc in the same change.
6. **Keep docs current.** Update `docs/roadmap.md` when a step is completed or a decision is made.
7. **Commit after each completed step** with a clear message.

## Key decisions (see design docs for detail)

| # | Decision |
|---|---|
| D1 | Moving weighted average cost; batches for traceability |
| D2 | ~80% B2B / 20% B2C default; industry packs override |
| D3 | One company GSTIN per state with a warehouse/store |
| D4 | One tax invoice per shipment at dispatch; counter sales invoiced at handover |
| D5 | `customer_payments` has a status; only successful payments are allocated |
| D6/D13 | One workspace per company: `scm_<code>_oltp` (clean), `scm_<code>_sim` (simulation), `data/duckdb/<code>.duckdb`; registry in `scm_control.workspaces` |
| D7 | Financial year April–March, configurable |
| D8 | MySQL 8.0.16+ installed natively (no Docker); Python 3.11+; app user `scm_app` limited to `scm\_%` schemas |
| D9 | Owner accepts architect recommendations; still ask before each major phase |
| D10 | Orders carry sales channel, marketing channel, optional campaign |
| D11 | Industry packs (YAML) drive catalogue, pricing, GST slabs, seasonality, channel mix |
| D12 | Streamlit control panel; generation runs as background job, progress in `generation_run_steps` |
| D14 | Re-run actions: Continue (append forward in time), Rebuild (drop + regenerate, same seed = same data), New workspace |
| D15 | All FKs `ON DELETE/UPDATE RESTRICT`; master data never hard-deleted (status or `deleted_at`) |
| D16 | No account codes in Python: accounts found via `chart_of_accounts.system_role`, `business_units` (revenue/returns/COGS), `tax_accounts`, `expense_categories`, `bank_accounts` |
| D17 | GST rate effective-dated per product (`product_tax_rates`); default schedule includes the 22 Sep 2025 rate change |
| D18 | One base UoM per product for all transactions (v1) |
| D19 | No circular FKs (no department head / warehouse manager columns) |
| D20 | Generator assigns surrogate IDs deterministically |
| D21 | `created_at`/`updated_at` set from the simulated clock; no `ON UPDATE CURRENT_TIMESTAMP` (provisional, Q4) |
| D22 | Opening balances only via an opening journal entry, never on master tables |
| D23 | D2's 80/20 B2B/B2C mix applies to net sales value, not customer counts |
| D24 | Generated emails/websites use the `.example` domain; bank names and IFSC prefixes are invented |
| D25 | Base currency is a workspace setting (default INR, one currency per workspace); tax model is GST-only in v1 |
| D26 | `inventory` = one row per warehouse × product with status quantity columns, reserved qty, stock value, reorder policy |
| D27 | Moving-average cost per warehouse × product; stock value stored, MAC = value ÷ qty |
| D28 | Ledger source linking: one nullable FK per source type + CHECK exactly one (no polymorphic source_type/source_id) |
| D29 | Stock transfers between GSTINs are taxable (IGST at MAC value); the transfer doc is the tax invoice |
| D30 | POs come from an RFQ award (line-level award, quotation FK) or a direct reorder to the preferred supplier |
| D31 | Dock inspection: only accepted qty enters stock; rejected goods never create a movement |
| D32 | Header totals and line counters are stored, and each has a reconciliation check |
| D33 | `inventory_transactions` is append-only; corrections are reversal rows |
| D34 | Opening stock is an `OPENING_STOCK` stock adjustment |
| D35 | Stock valued at PO price; supplier-invoice price differences go to Purchase Price Variance |
| D36 | All generated PANs start with the fixed synthetic prefix `ZZX`; GSTINs remain fully valid (format + check digit) |
| D37 | No product price history table; order lines snapshot list and charged price (history via dbt snapshots/CDC) |
| D38 | GST tax documents (invoice, credit note, transfer invoice) use ≤16-char numbers `<STATE><FY4>-<T><SEQ6>` |
| D39 | Orders carry total GST only; CGST/SGST vs IGST split is decided per invoice |
| D40 | Invoices can be cancelled only within 24 h and before in-transit; otherwise credit notes |
| D41 | Return to origin: `RTO_RECEIPT` stock movement + RTO credit note |
| D42 | Shipping charge billed once as a `SHIPPING_CHARGE` invoice line at the invoice's highest goods GST rate |
| D43 | COD: carrier collects cash → `customer_payments` row with carrier and remittance `settlement_date` |
| D44 | Return credit notes issued after final inspection for all received qty; refunds pay out credit notes |
| D45 | `defective_stock` register, one row per defect event, exactly-one source FK, whole-row disposition |
| D46 | e-invoice IRN on invoices/credit notes to GST-registered customers |
| D47 | B2B credit hold (`ON_HOLD`) on credit-limit or overdue breach |
| D48 | Line chain: shipment line N:1 order line; invoice line 1:1 shipment line (or order line for handover); return/credit lines N:1 invoice line |
| D49 | AR/AP are open-item subledgers incl. payments (and refunds); allocations clear items and post no journal |
| D50 | `journal_entries`: one nullable FK per source + `journal_source` + generated UNIQUE `posting_key` (no double posting) |
| D51 | AP item = exactly one of supplier invoice / expense / supplier payment; payee supplier or employee |
| D52 | COGS posted inside the sales invoice journal |
| D53 | UPI/card/net-banking/COD receipts go to Payment Clearing; bank settlement lines post Bank + gateway fees |
| D54 | No year-end closing journal; P&L reset and retained earnings handled in `gl_period_balances` |
| D55 | Inter-GSTIN transfer: one journal Dr Input IGST (dest) / Cr Output IGST (source), no branch accounts |
| D56 | Monthly PAYROLL expense → Salaries Payable; statutory deductions deferred (R27) |
| D57 | All non-payroll expenses (incl. petty cash, employee claims) go through AP |
| D58 | GSTR-1 and GSTR-3B per GSTIN per month; 3B filing posts the settlement with legal ITC set-off order |
| D59 | Posting date = event date if its period is open, else first day of earliest open period |
| D60 | `defective_stock.repair_cost_amount` is a memo; not posted |
| D61 | Posted journals are immutable; corrections via REVERSAL journals |
| D62 | Engine refuses unbalanced journals; header debit = credit CHECK; lines re-summed in validation |
| D63 | `customer_payments.settlement_bank_transaction_id` links clearing receipts to their bank settlement |

## Database standards

- InnoDB, `utf8mb4`, `utf8mb4_0900_ai_ci`. Tables `snake_case` plural.
- Surrogate `BIGINT UNSIGNED AUTO_INCREMENT` PKs named `<entity>_id`; human-readable document numbers with UNIQUE constraints.
- Explicit FK constraints always. Indexes on FKs and frequent filters.
- Money `DECIMAL(18,2)`, unit prices/costs `DECIMAL(18,4)`, quantities `DECIMAL(18,3)`, rates `DECIMAL(7,4)`. Never FLOAT for money.
- Status columns `VARCHAR(30)` + `CHECK`.
- `created_at`, `updated_at` `DATETIME(6)` on every table; `deleted_at` on soft-deletable master data.
- Business dates (`order_date`, `invoice_date`…) are separate from audit timestamps.
- `journal_entry_lines` is the only accounting source of truth; general ledger is derived.
- `inventory_transactions` is the only inventory movement ledger; `inventory` balances must reconcile to it.

## Data generation standards

- Master data first, then transactions as consequences of business events, in chronological order.
- Faker only for master data attributes; transactions come from business logic.
- Every journal entry balances. Inventory never goes negative. Returns never exceed delivered quantity.
- Deterministic with a seed. Batch inserts, not row-by-row.
- **Default mode always produces clean, valid data.** Anomalies only in the explicit Anomaly Simulation Mode, isolated in `scm_<code>_sim`. Never silently corrupt clean data.
- All names, addresses and GSTINs are fictional.

## Python standards

- Modular packages; never one giant script. PEP 8, type hints, meaningful names.
- Config in YAML (`config/`), secrets in `.env` (never committed). No hardcoded credentials or business rules that belong in config.
- Structured logging, proper exception handling, no duplicated logic.
- Tests with pytest: unit, integration (real MySQL), constraint, business-rule and reconciliation tests.

## ETL / DuckDB standards

- MySQL is the source of truth; DuckDB is the analytical replica only. No business data generated in DuckDB.
- Generic, reusable loader (not one function per table). Full, incremental and table-level reloads.
- Watermarks, load metadata (`etl_load_history`, `etl_table_watermarks`, `etl_error_log`), idempotent reruns, no duplicates, source-vs-target validation.
- Design for later Airflow/dbt integration without implementing them early.

## Communication

The owner is learning Data Engineering and Analytics Engineering. Explain important decisions in practical terms with realistic examples; when there are alternatives, recommend one and state the trade-offs. Don't overwhelm.

Delivery checklist for every feature: what is implemented, files changed, how it fits, how to run, expected output, how to test, dependencies/config changes.
