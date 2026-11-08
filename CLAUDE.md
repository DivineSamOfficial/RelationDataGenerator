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
