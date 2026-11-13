# Owner testing guide

A walkthrough for testing the finished platform, about 1–2 hours in all. Each step says what to run, what you should see, and what it teaches. Commands assume the virtual environment is active (`source .venv/bin/activate`).

A ready-made workspace **`demo`** exists: Small preset, electronics + FMCG, seed 42, data from 2025-04-01 to 2025-09-30, already loaded into `data/duckdb/demo.duckdb`. A Medium workspace **`m4`** (two years, 4.5 million rows) exists too.

## 1. Control panel (15 min)

```bash
scmgen ui
```
Open http://localhost:8501 and pick `demo` in the sidebar.

- **Data explorer → Charts:** orders per month by channel, revenue by business unit, stock value by warehouse. The seasonality (festive months) and the B2B / B2C channel mix come from the industry packs.
- **Table guide:** pick `orders`. You get its columns (from the design), the tables it references and the tables that reference it (diagram), how it's populated, and example queries you can run with *Run on MySQL*. Try `journal_entry_lines` and `inventory_transactions`, the two ledgers everything reconciles to.
- **Validation:** the last report: 99 rules passed, 2 not applicable.
- **Scenarios:** the 20 business scenarios of the master prompt with their measured share, e.g. cancelled orders, partial deliveries, failed payments.

## 2. Generate and continue (15 min)

In *Workspaces*, create a workspace (e.g. `test1`) and generate history (Small preset, a few months). Watch *Run monitor*: progress per day and per step, rows per second. Then:

- **Continue** with 7 days: only the new days are simulated (the *Plan* line says which).
- From the CLI: `scmgen generate plan -w test1` (what Continue would do) and `scmgen generate rebuild -w test1 --confirm test1` (drops and regenerates: same seed ⇒ the same data).

What it teaches: historical vs continuous generation, and why reproducibility (a seed) matters for testing pipelines.

## 3. Incremental loading (15 min)

```bash
scmgen load -w test1             # first time: FULL
scmgen generate continue -w test1 --days 3
scmgen load -w test1             # INCREMENTAL: only rows changed after the watermark
scmgen load status -w test1      # watermark per table, last load, reconciliation
```
Look at `etl_meta.etl_load_history` in the DuckDB file: rows extracted / inserted / updated per table and load. Run `scmgen load -w test1` again: nothing moves (idempotent).

What it teaches: APPEND vs MERGE strategies (`config/loader/tables.yaml`), watermarks committed with each batch, reconciliation after every load.

## 4. CDC and anomalies (30 min)

In the control panel, page *Simulation* (or the CLI, see the README):

1. *Reset staging copies*, then *Load sim replica* and *CDC capture*, then *CDC apply*. The first capture is a snapshot.
2. `scmgen generate continue -w demo --days 2`, then capture and apply again: now you get INSERT / UPDATE events with before-images, and `cdc_history.orders` in `demo_sim.duckdb` keeps every version of an order (SCD Type 2: `_valid_from`, `_valid_to`, `_is_current`).
3. Inject the scenarios one by one (`data_quality`, `business_rules`, `pipeline`, `cdc_stream`) and run the *Detection report*. Every anomaly should be CAUGHT by the checks it lists, with no unexplained failure.

What it teaches: business events vs change events, duplicate and out-of-order events, late-arriving data, silent updates a watermark can't see, schema drift, and how data-quality tests (not-null, unique, relationships) complement business-rule checks.

## 5. Your own SQL (open-ended)

```python
import duckdb
con = duckdb.connect("data/duckdb/demo.duckdb", read_only=True)
con.sql("""
    SELECT a.account_type_code, SUM(l.debit_amount - l.credit_amount) AS balance
    FROM raw.journal_entry_lines l JOIN raw.chart_of_accounts c ON c.account_id = l.account_id
    JOIN raw.account_types a ON a.account_type_id = c.account_type_id
    GROUP BY 1 ORDER BY 1""").show()
```
Debits and credits balance to zero across all accounts: double-entry accounting holds in the generated data.

## What to report back

Anything that looks unrealistic (amounts, volumes, timing, names), anything that is hard to understand in the UI or the Table guide, and any error. Known open item: the Medium preset takes about 17–18 minutes for two years, against a 15-minute target.
