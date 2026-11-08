# Roadmap and Status

Last updated: 2026-11-08

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture & database design | **Done — approved by owner 2026-11-08** (incl. interpretations I1–I6, Part 1H §4) |
| 2 | Database implementation (MySQL schema, constraints, indexes, workspace management) | **In progress** — approved 2026-11-08 |
| 3 | Data generation engine (master data, transactions, industry packs, scenarios, historical + continuous, progress reporting) | Not started |
| 4 | DuckDB loader (full, incremental, metadata, reconciliation) | Not started |
| 5 | Control Panel UI (Streamlit) | Not started |
| 6 | Testing consolidation & documentation | Not started |
| 7 | Advanced simulation (CDC, anomalies, late data, schema evolution, recovery) | Not started |

## Phase 1 parts

| Part | Content | Status |
|---|---|---|
| 1A | Business overview, entity inventory, decisions, addenda A1 (attribution, stores, industry packs, UI) and A2 (workspaces, re-run behaviour) | **Done** — `docs/design/phase1_part1a_business_and_entities.md` |
| 1B | Master data + accounting master tables (31 tables): full column definitions, keys, constraints, relationships, population order and sources, default COA, validation rules; decisions D15–D24 | **Done** — `docs/design/phase1_part1b_master_data_and_accounting_master.md` |
| 1C | Procurement (16) + inventory (8) tables: columns, keys, constraints, flows, moving-average valuation rules, validation rules; decisions D25–D35; resolved Q1, Q2 (inventory), Q7, Q8; `inventory` grain changed to warehouse × product (D26, supersedes 1A §5 D); 1B amended for configurable currency (D25) | **Done** — `docs/design/phase1_part1c_procurement_and_inventory.md` |
| 1D | Sales (4), shipping (5), invoicing (4), returns/refunds/defective (5) tables: columns, keys, constraints, order-to-cash and returns flows, validation rules; decisions D37–D48; resolved Q6; amended 1C (compact GST document numbers D38, RTO movement D41, ledger volume) | **Done** — `docs/design/phase1_part1d_sales_shipping_invoicing_returns.md` |
| 1E | Expenses (4), journals/ledger (3 + view), bank (2), AR (2), AP (2), tax (3) tables; posting rules P01–P25 for every event; reconciliations; decisions D49–D63; resolved Q2 (journals) and Q3; AR/AP refined to open items incl. payments (D49, refines 1A §5 J); amended 1D (D60, D63) | **Done** — `docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md` |
| 1F | ERD by module: overview, 12 module diagrams, journal-source diagram, traceability flow (Mermaid, 16 diagrams, all parsed and validated); table index of all 92 tables | **Done** — `docs/design/phase1_part1f_erd.md` |
| 1G | Daily generation engine (historical + continuous, determinism, restart safety), workspaces/re-runs, system tables + `scm_control.workspaces` (all 92 tables now designed), DuckDB loader (full/append/merge, keyset watermarks, metadata, reconciliation, recovery), per-table load strategy, CDC and anomaly architecture, code structure, CLI; decisions D64–D77; resolved Q4, Q5; refines A2 (day-transaction rollback D64, per-day seeds D65) | **Done** — `docs/design/phase1_part1g_generation_loader_cdc_architecture.md` |
| 1H | Design review: requirements traceability, fixes F1–F5 applied (D78–D81: cash-flow category, tax-inclusive B2C pricing, composite FKs on snapshot columns, FK declaration), deferrals, interpretations I1–I6 for owner confirmation, MySQL assumptions to prove in Phase 2, risks, Phase 2 scope proposal | **Done** — `docs/design/phase1_part1h_design_review.md` |

## Phase 2 steps (scope from Part 1H §8)

| Step | Content | Status |
|---|---|---|
| 2.1 | Project skeleton: `pyproject.toml`, package layout, settings + `.env`, logging, pytest, MySQL connectivity check | **Done** — `scmgen db check` passes on MySQL 26.10.0; 25 tests pass (22 unit, 3 integration) |

| 2.2 | MySQL users/privileges (`scm_app`, `scm_reader`, `scm_anomaly`), `scm_control.workspaces` | **Done** — `scmgen db init` (idempotent); wildcard grants verified on MySQL 26.10 by a throwaway-user probe; privilege matrix proven both ways by tests; 60 tests pass. Finding: PyMySQL %-formats statements even via `exec_driver_sql`, so SQL containing `%` runs through `execute_raw` (no parameters) |
| 2.3 | Assumption tests A1–A7 on the installed MySQL (A1/A2 already seen working on `workspaces`: CHECK + `REGEXP_LIKE` enforced) | Next |
| 2.4 | DDL for all 92 tables + `v_general_ledger` | |
| 2.5 | Workspace commands (`db init`, `workspace create/list/delete`) | |
| 2.6 | Constraint test suite | |
| 2.7 | Schema-vs-design check | |

## Open questions

| # | Question | Part |
|---|---|---|
| Q1 | Where moving-average cost is stored | **Resolved** 1C (D26/D27) |
| Q2 | Linking journal entries / inventory transactions to source documents | **Resolved**: inventory 1C (D28), journals 1E (D50) |
| Q3 | AP open items referencing a supplier invoice or an expense | **Resolved** 1E (D51) |
| Q4 | Simulated clock vs `updated_at` and watermarks | **Resolved** 1G (D67; confirms D21) |
| Q5 | Append-only vs mutable vs full-reload tables | **Resolved** 1G §7 (D69: 24 FULL / 21 APPEND / 47 MERGE) |
| Q6 | Product list-price history table or in-place price updates | **Resolved** 1D (D37: in place; history via dbt snapshots/CDC) |
| Q7 | GST treatment and documents for stock transfers between different GSTINs | **Resolved** 1C (D29) |
| Q8 | Per-warehouse reorder level/quantity | **Resolved** 1C (D26) |
| Q9 | Guaranteeing generated GSTINs are never real ones (master prompt §3) | **Resolved**: Option A, D36 (fixed `ZZX` PAN prefix) |

Details: Part 1A §7 (Q1–Q5), Part 1B §1.3 (Q6–Q8), Part 1C §1.4 (Q9).

## Deferred scope (v1)

R22 purchase returns / debit notes (1A); R23 GST cess, R24 TDS, R25 multi-GSTIN customers, R26 imports / multi-currency / reverse charge (1B §1.2); R27 payroll statutory deductions, R28 fixed-asset register (1E §1); R29 GST TCS on marketplace sales (1H §3).

## Environment checklist (owner)

- [ ] Git, VS Code, Claude Code extension
- [x] MySQL installed natively (Homebrew) and running — server 26.10.0, InnoDB, utf8mb4_0900_ai_ci, strict mode, binlog ROW (checked 2026-11-08)
- [x] Python 3.11+ — 3.12.9 in `.venv`
- [x] Paste the full master prompt into `docs/master_prompt.md`
- [ ] Optional: DBeaver or MySQL Workbench for browsing data
