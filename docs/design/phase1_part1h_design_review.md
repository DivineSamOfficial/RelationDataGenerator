# Phase 1 – Part 1H: Design Review and Phase 1 Summary

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Review complete. **Phase 1 awaiting owner approval** (CLAUDE.md rule 1, D9)
**Last updated:** 2026-11-08
**Reviews:** Parts 1A–1G against `docs/master_prompt.md`

---

## 0. What This Review Did

1. **Traced every master-prompt requirement** to the part of the design that satisfies it (§1).
2. **Re-read Parts 1A–1G for internal consistency**: rules that contradict each other, redundant data without protection, missing pieces needed by the required reports, and assumptions about MySQL behaviour that haven't been proven yet.
3. **Fixed what should be fixed now** (§2), updating the affected design documents in the same commit (change-management rule).
4. Listed **remaining deferrals, interpretations the owner should confirm, and risks** (§3–§6).
5. Summarised **the whole design** (§7) and proposed **the scope of Phase 2** (§8) for approval.

Earlier mechanical checks still stand: all 16 ERD diagrams and both 1G diagrams parse in Mermaid 11.4.1; the 92 tables in the 1F index are exactly the 92 classified by load strategy in 1G §7.

---

## 1. Requirements Traceability (master prompt)

### 1.1 Phase 1 deliverables (§17)

| # | Required | Where |
|---|---|---|
| 1 | Business-process overview | 1A §1–§2 |
| 2 | Complete list of entities and tables | 1A §5 (+ A1/A2), 1F §1 index (92 tables) |
| 3 | Detailed table definitions and column descriptions | 1B (31), 1C (24), 1D (18), 1E (16), 1G §5 (3) |
| 4 | PK and FK relationships | Each part's tables + relationship summaries (1B §6, 1C §6, 1D §7, 1E §8) |
| 5 | Cardinalities | Same summaries + 1F diagrams |
| 6 | ERD by module | 1F (overview + 12 modules + journal sources + traceability) |
| 7 | Procurement flow | 1C §3.0 |
| 8 | Sales and order flow | 1D §2 |
| 9 | Inventory movement flow | 1C §4 (valuation), §5.0 (movement map) |
| 10 | Returns and refunds flow | 1D §2, §6 |
| 11 | Financial accounting flow (operational → double entry) | 1E §6 (P01–P25 + worked example) |
| 12 | Historical and continuous generation | 1G §2–§3 |
| 13 | DuckDB loading (full, incremental, watermarks, CDC) | 1G §6–§8 |
| 14 | CDC and anomaly architecture (clean data protection) | 1G §8–§9 |
| 15 | Redundant / missing entities | 1A §4 (R1–R22), this review §2 |

### 1.2 Module requirements (§4–§10, §14–§15)

| Requirement | Status |
|---|---|
| All requested tables (§4 A–I) | Present, or merged/dropped with reasons (1A §4: R1–R7) |
| Multi-line orders, statuses, discounts, taxes, partial/multiple/failed payments (§4B) | 1D §3 |
| Inventory per warehouse × product, reorder levels, reconciliation to movements (§4C) | 1C §5 (D26), V-IN-01 |
| Invoices: GST, discounts, partial invoicing, cancellation, credit notes, outstanding (§4D) | 1D §5 (D39–D40), 1E AR |
| Returns: eligibility, inspections, partial refunds, defective with reason and movements (§4E) | 1D §6 (D44–D45) |
| Shipping statuses list, split shipments, tracking (§4F) | 1D §4 (R11, R12) |
| Full procurement lifecycle, partial receipts, rejected goods separately (§4G) | 1C §3 (D30–D31) |
| Expenses incl. AI subscriptions, petty cash, recurring/one-off, attachments (§4H) | 1E §2; GST payments correctly *not* expenses (R10) |
| Double entry, reversals, periods, operational linkage, GL as derived (§4I) | 1E §3, §6 (D50, D61, R6) |
| Reports supportable (§4J): TB, P&L, BS, AR/AP ageing, GL, outstanding, GST in/out, inventory valuation, COGS | Supported. **Cash-flow statement needed one addition** (F1 below) |
| Business-aware generation, consequences of events (§5) | 1G §2 (D66), 1E §0 |
| Scenarios with configurable distributions (§6) | 1G §2.3 |
| Configurable volumes, dates, seed, batch size, incremental generation (§7) | 1G §2–§3, §11 |
| MySQL DDL, constraints, batch inserts, scripts (§8) | Phase 2 / 1G §11 |
| Generic DuckDB loader, metadata tables, watermark edge cases, alternatives for tables without timestamps (§9) | 1G §6–§7 (every table has reliable `updated_at`, D67) |
| Data-quality checks (§10) | Covered by V-* rules in 1B–1G (each §10 item maps to at least one rule) |
| Clean mode always valid; separate anomaly mode (§14) | D76 gate; 1G §9 (D73) |
| Traceability, CDC, anomalies, reconciliation, reproducibility (§15) | 1F §16; 1G §8–§9; D65 |

**Result:** every requirement is covered. Two are covered by a deliberate interpretation that the owner should confirm (§4).

---

## 2. Findings Fixed in This Review

Each fix is a column, key or rule refinement. **No tables are added; the count stays 92.** The design documents named are updated in this commit.

| # | Finding | Fix | Updated |
|---|---|---|---|
| F1 | **Cash-flow statement** (§4J) can't be built from the GL alone: nothing says whether a balance-sheet account's movement is operating, investing or financing | New column `chart_of_accounts.cash_flow_category`: `CASH` (bank, petty cash) / `OPERATING` (receivables, inventory, payables, GST, GRNI, clearing, advances, accruals, accumulated depreciation as a non-cash add-back) / `INVESTING` (fixed assets) / `FINANCING` (share capital, cash-credit facility) / `NOT_APPLICABLE` (P&L accounts, retained earnings, opening balance equity; profit enters the statement as net profit). This supports the indirect method from `gl_period_balances`, and the direct method from `bank_transactions.transaction_type` | 1B §4.9 (D78) |
| F2 | **B2C prices are tax-inclusive in India** (shelf / MRP price, e.g. ₹499 including GST). The design added GST on top of an ex-GST price, which produces unrealistic B2C totals like ₹588.82. It also left prepaid B2C orders exposed to a GST rate change between order and dispatch | `orders.prices_include_tax` (TRUE for B2C, FALSE for B2B) and `order_line_items.inclusive_unit_price` (the price point including GST, fixed at order time). For inclusive lines, `taxable_amount = ROUND(qty × inclusive × (1 − disc%) / (1 + rate/100), 2)` and tax follows 1C §2.3, so the invoice's round-off brings the total back to the shown price. If the GST rate changes before dispatch, the invoice recomputes taxable value at the invoice-date rate and **the customer still pays the same price**, so prepayments always match. V-IV-02 is amended: for inclusive lines the invoice's ex-GST unit price may differ from the order line's, and the inclusive price must not | 1D §3 (D79) |
| F3 | **Snapshot / convenience columns that duplicate a parent's value were only protected by validation.** Examples: `invoices.customer_id` vs the order's customer; `credit_notes.customer_id` and GSTIN vs the invoice's; `supplier_invoices.supplier_id` vs the PO's; an invoice line's product vs its order line's | Enforce them with **composite foreign keys** (the 1B §2.4a technique): `invoices (order_id, customer_id)` → `orders`; `customer_payments (order_id, customer_id)` → `orders`; `credit_notes (invoice_id, customer_id, company_gst_registration_id)` → `invoices`; `supplier_invoices (purchase_order_id, supplier_id)` → `purchase_order_headers`; `invoice_line_items (order_line_item_id, product_id)` → `order_line_items`; `sales_return_line_items (invoice_line_item_id, order_line_item_id)` → `invoice_line_items`. Each parent gets the matching UNIQUE key | 1C §3, 1D §3/§5/§6 (D80) |
| F4 | **MySQL may reject explicit `ON DELETE RESTRICT` on FK columns that are also used in CHECK constraints** (MySQL restricts referential actions on such columns, and many of our CHECKs reference FK columns). D15 said every FK is declared `ON DELETE RESTRICT ON UPDATE RESTRICT` | Declare FKs **without action clauses**. InnoDB's default is `NO ACTION`, which InnoDB enforces exactly like `RESTRICT` (checked immediately). The behaviour is the same as D15 and avoids the conflict. Phase 2 tests both forms and records the result | 1B D15 wording (D81) |
| F5 | `shipment_tracking` has both `received_at` (when the feed reached us) and the standard `created_at`. The design didn't say they're equal, and loader correctness (D67) needs `created_at` to be the recording time | Rule: `created_at = received_at` for tracking rows. Clean mode keeps `received_at` monotonic per the clock, as for all audit timestamps | 1D §4 |

---

## 3. Deferred Scope (consolidated)

Each can be added later without restructuring existing tables.

| # | Item | Where noted |
|---|---|---|
| R22 | Purchase returns / supplier debit notes (defective RETURN_TO_SUPPLIER is written off in v1) | 1A |
| R23 | GST compensation cess | 1B |
| R24 | TDS on vendor payments | 1B |
| R25 | Multi-GSTIN B2B customers | 1B |
| R26 | Imports, multi-currency transactions, reverse charge | 1B (amended by D25) |
| R27 | Payroll statutory deductions (PF, ESI, TDS, professional tax) | 1E |
| R28 | Fixed-asset register (opening balances + monthly depreciation only) | 1E |
| **R29** | **GST TCS on marketplace sales.** Marketplaces collect tax at source (section 52) on the seller's behalf. In v1 the marketplace is a sales channel with a commission expense only; TCS credit isn't modelled | **New, this review** |
| — | Control-panel login / users (local single-user tool) | This review |

---

## 4. Interpretations the Owner Should Confirm

These aren't defects. They're places where the master prompt could be read more than one way, and the design chose the reading that keeps data consistent. **Approving Phase 1 confirms these unless you say otherwise.**

| # | Master prompt says | Design does | Why |
|---|---|---|---|
| I1 | CLI to "generate only sales data / only procurement data / only accounting data" (§12) | Module-only generation exists for development and testing (in a scratch workspace with minimal upstream data). Normal data always comes from the full daily engine; `validate` and `load` can target one module (1G §11) | Accounting must be a consequence of events (§4I), and sales need stock from procurement. Separately generated modules would contradict each other |
| I2 | "Load generated data into MySQL" as a separate script (§8) | The engine writes straight into MySQL inside day transactions; there's no intermediate file stage | Writing directly keeps day-level atomicity (D64) and avoids a second copy of the data. An export command can be added if files are wanted for other exercises |
| I3 | Example volumes: 1,000 customers, 100 suppliers, 500 products, 50,000 orders (§7) | Medium preset: ~50,000 orders over 2 years, with ~7,000 customers (mostly B2C), ~260 suppliers, ~2,000 products. A "Master prompt example" preset with exactly the §7 numbers will also exist | B2C-heavy packs need far more customers than orders per customer would suggest. Both presets are available |
| I4 | "80% B2B / 20% B2C" (D2) | Applied to sales **value**, not customer counts (D23) | Realistic for a distributor |
| I5 | "Never use actual GSTINs" (§3) | Fully valid synthetic GSTINs with a fixed `ZZX` PAN prefix; small residual collision risk accepted (D36, owner decision) | Owner chose Option A |
| I6 | "Soft deletions where appropriate" (§15C) and CDC DELETE events | The clean schema never hard-deletes (D15). Deletes appear only as soft deletes (`deleted_at`) or, in anomaly scenarios, as CDC DELETE events in `_sim` | Hard deletes would break ledgers and audit trails in an ERP |

---

## 5. Assumptions Phase 2 Must Prove (not assumed)

Phase 2 writes one test per item against the installed MySQL version and records the outcome in the roadmap. Any failure is redesigned through the change process before continuing.

| # | Assumption | Used by |
|---|---|---|
| A1 | `CHECK` constraints are enforced (MySQL ≥ 8.0.16) | Everywhere |
| A2 | `REGEXP` is allowed inside `CHECK` | GSTIN, PAN, IFSC, PIN, HSN formats (1B §2.3) |
| A3 | Stored generated columns can carry UNIQUE indexes, and CHECKs can reference them | "At most one default/current/preferred" keys (1B §2.4b), `posting_key` (D50) |
| A4 | A composite FK whose columns are partly NULL is not checked | Nullable composite self-FKs (1B), F3 keys |
| A5 | FKs without action clauses behave as RESTRICT, and coexist with CHECKs on the same columns (F4) | D15/D81 |
| A6 | CHECK expressions with date arithmetic (`issued_at + INTERVAL 24 HOUR`) and multi-column boolean sums are accepted | D40, exactly-one-source CHECKs (D28, D50) |
| A7 | Explicit values can be inserted into `AUTO_INCREMENT` PKs, and the counter continues past them | D20 |
| A8 | A day-sized transaction (Large preset, tens of thousands of rows) commits within acceptable time and memory | D64 |
| A9 | DuckDB supports the chosen types (`UBIGINT`, `DECIMAL(18,4)`, `JSON`) and `INSERT OR REPLACE` on PK tables (pin the version) | 1G §6 (Phase 4) |

---

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Phase 3 scope is large** (92 tables, ~25 posting rules, ~100 validation rules) | Long phase with a big-bang integration risk | Build the engine in **vertical slices**, each ending with its validations passing: (1) master data + opening balances; (2) procurement + inventory + their journals; (3) sales + shipping + invoicing + AR; (4) returns + defective; (5) expenses + bank + AP; (6) GST returns + period close. Every slice is a separate approved step |
| Constraint-heavy schema slows inserts | Generation time | Day-level batches; measure in Phase 3 (A8); keep indexes (correctness first, master prompt §14) |
| GST / Indian rules change (thresholds, rates) | Data looks dated | All thresholds and rates are config (B2CL limit, e-way bill limit, rate schedule). The documentation says the tax logic is educational, not compliance advice |
| Realism tuning (seasonality, rates) is subjective | Data "feels" off | Plan preview + data explorer (Phase 5) make distributions visible; packs are YAML and easy to tune |
| Determinism broken by an innocent change (dict order, unseeded randomness, wall-clock use) | Rebuild no longer reproducible | V-SY-04 double-build checksum test in CI from Phase 3; only the `core` clock and RNG modules may produce time and randomness |
| Loader correctness under concurrency | Missed or duplicate rows | High-water mark + keyset watermark + atomic batch commits (D67, D71); a Phase 4 test loads while generation runs and compares |
| Owner learning load | Overwhelm | Small steps, a plan before code, an explanation with every delivery (CLAUDE.md) |

---

## 7. Phase 1 Summary

### 7.1 What exists

- **92 tables + 1 view** in each workspace's clean schema `scm_<code>_oltp`, plus `scm_control.workspaces`, the `_sim` schema (Phase 7 tables) and DuckDB `etl_meta`.
- **81 decisions** (D1–D81), all listed in `CLAUDE.md` and explained in the parts.
- **9 open questions raised and resolved** (Q1–Q9).
- **104 validation rules** (counted across 1B–1G: V-MD, V-AC, V-PR, V-IN, V-SO, V-PY, V-SH, V-IV, V-RT, V-EX, V-AR, V-AP, V-TX, V-BK, V-SY, V-LD), most of which double as master-prompt reconciliations.

### 7.2 The design in ten sentences

1. A fictional Indian distributor with one GSTIN per state, DCs and stores, configurable industries (packs), and workspaces per company.
2. Master data is versioned where history matters (addresses, GST rates) and never hard-deleted.
3. Procurement runs RFQ → line-level award → PO → partial deliveries → dock inspection → three-way-matched supplier invoice → payment.
4. One inventory ledger records every quantity change with exactly one source document. Balances per warehouse × product carry status buckets, reservations and a value-based moving-average cost.
5. Orders carry attribution; shipments are split per warehouse; a tax invoice is raised at dispatch with COGS posted in the same journal.
6. Returns flow through inspection into sellable or defective stock, credit notes and refunds. RTO, COD and e-invoicing behave as they do in India.
7. Every financially relevant event posts exactly one balanced, immutable journal (posting key prevents duplicates). AR and AP are open-item subledgers equal to their control accounts. GST is recorded per line and settled monthly through GSTR-3B.
8. The engine simulates one day per transaction, with seeds keyed by date, so data is valid after any crash and identical across any run split.
9. A generic DuckDB loader replicates FULL / APPEND / MERGE tables with keyset watermarks below a committed high-water mark, and reconciles every load.
10. CDC events and anomalies live only in the `_sim` schema and a separate DuckDB file, written by a user that can't modify clean data, and every anomaly declares the checks it must fail.

---

## 8. Proposed Phase 2 Scope (for approval; not started)

**Goal:** a MySQL schema that implements Parts 1B–1G exactly, proven by tests. No data generation.

| Step | Deliverable |
|---|---|
| 2.1 | Project skeleton: `pyproject.toml`, package layout (1G §10), settings + `.env` loading, logging, pytest setup, a MySQL connectivity check |
| 2.2 | MySQL users and privileges script (`scm_app`, `scm_reader`, `scm_anomaly`), `scm_control` schema, `workspaces` table |
| 2.3 | Assumption tests A1–A7 (§5) on the installed MySQL; results recorded |
| 2.4 | DDL for all 92 tables as versioned SQL files per module (in FK dependency order), with every PK, FK, UNIQUE, CHECK, generated column and index from the design. The `v_general_ledger` view |
| 2.5 | Workspace commands: `db init`, `workspace create` (schemas + DDL only, no generation yet), `workspace list`, `workspace delete` |
| 2.6 | Constraint test suite: for each table, a valid row inserts; each CHECK / UNIQUE / FK rejects a crafted invalid row; drop and recreate is idempotent |
| 2.7 | Schema-vs-design check: a script compares `information_schema` with the table/column lists in the design docs and reports differences |

Each step follows the CLAUDE.md process: plan → implement → run tests (with real output) → commit → update the roadmap.

**Approval requested:** please confirm (a) Phase 1 is approved as the baseline, including interpretations I1–I6, and (b) Phase 2 may start with step 2.1.
