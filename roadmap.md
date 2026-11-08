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
| 1B | Master data + accounting master tables: full column definitions | Next |
| 1C | Procurement + inventory tables; resolve Q1 (where moving-average cost lives), Q2 (source-document linking) | |
| 1D | Sales, shipping, invoicing, returns tables | |
| 1E | Expenses, accounting transactions, AR/AP, tax; double-entry posting rules per business event; Q3 | |
| 1F | ERD by module | |
| 1G | Historical/continuous generation, workspaces, DuckDB loader, CDC & anomaly architecture; Q4, Q5 | |
| 1H | Full design review: gaps, risks | |

## Open questions

See §7 of the Part 1A doc (Q1–Q5).

## Environment checklist (owner)

- [ ] Git, VS Code, Claude Code extension
- [ ] Python 3.11+
- [ ] MySQL 8.0.16+ installed natively and running
- [ ] Optional: DBeaver or MySQL Workbench for browsing data
