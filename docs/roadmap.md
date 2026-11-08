# Roadmap and Status

Last updated: 2026-11-08

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture & database design | **In progress** |
| 2 | Database implementation (MySQL schema, constraints, indexes, workspace management) | Not started — needs approval |
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
| 1D | Sales, shipping, invoicing, returns tables (incl. `defective_stock` and the FKs forward-referenced by 1C); Q6 (product price history) | Next |
| 1E | Expenses, accounting transactions, AR/AP, tax; double-entry posting rules per business event; Q3 | |
| 1F | ERD by module | |
| 1G | Historical/continuous generation, workspaces, DuckDB loader, CDC & anomaly architecture; Q4, Q5 | |
| 1H | Full design review: gaps, risks | |

## Open questions

| # | Question | Part |
|---|---|---|
| Q1 | Where moving-average cost is stored | **Resolved** 1C (D26/D27) |
| Q2 | Linking journal entries / inventory transactions to source documents | Inventory **resolved** 1C (D28); journals 1E |
| Q3 | AP open items referencing a supplier invoice or an expense | 1E |
| Q4 | Simulated clock vs `updated_at` and watermarks (D21 is provisional until this is settled) | 1G |
| Q5 | Append-only vs mutable vs full-reload tables | 1G |
| Q6 | Product list-price history table or in-place price updates | 1D |
| Q7 | GST treatment and documents for stock transfers between different GSTINs | **Resolved** 1C (D29) |
| Q8 | Per-warehouse reorder level/quantity | **Resolved** 1C (D26) |
| Q9 | Guaranteeing generated GSTINs are never real ones (master prompt §3) | **Resolved**: Option A, D36 (fixed `ZZX` PAN prefix) |

Details: Part 1A §7 (Q1–Q5), Part 1B §1.3 (Q6–Q8), Part 1C §1.4 (Q9).

## Deferred scope (v1)

R22 purchase returns / debit notes (1A); R23 GST cess, R24 TDS, R25 multi-GSTIN customers, R26 imports / foreign currency / reverse charge (1B §1.2).

## Environment checklist (owner)

- [ ] Git, VS Code, Claude Code extension
- [ ] Python 3.11+
- [ ] MySQL 8.0.16+ installed natively and running
- [x] Paste the full master prompt into `docs/master_prompt.md`
- [ ] Optional: DBeaver or MySQL Workbench for browsing data
