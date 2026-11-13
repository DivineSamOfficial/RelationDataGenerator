# RelationDataGenerator

A synthetic data platform that simulates the operational database (OLTP) of a fictional Indian supply chain and trading company — procurement, inventory, sales, shipping, returns, expenses, GST and double-entry accounting — and replicates it into DuckDB for data engineering and analytics practice: SQL, ETL, incremental loads, CDC, data quality, and later dbt / Airflow.

All company, person and tax identifiers it generates are fictional (PANs start with `ZZX`, e-mail domains are `.example`, bank codes are invented).

## Status

All seven phases are implemented and awaiting the owner's testing. See [`docs/roadmap.md`](docs/roadmap.md) for every step, its measured results and the open items (the Medium preset takes 17–18 minutes against a 15-minute target).

| Phase | What you get |
|---|---|
| 1–2 | The design ([`docs/design/`](docs/design/)) and a MySQL schema of 92 tables + 1 view, with constraints that make inconsistent data impossible |
| 3 | A daily simulation engine: master data, then every business event day by day, with balanced accounting, inventory that never goes negative, 99 validation rules and 20 business scenarios |
| 4 | A generic MySQL → DuckDB loader: full, incremental (append / merge by watermark), per-table reload, reconciliation, load metadata |
| 5 | A Streamlit control panel, including a **Table guide** for every table |
| 7 | CDC (change events + an SCD2 consumer) and anomaly simulation with a detection report, isolated from the clean data |

## Documentation

- Requirements: [`docs/master_prompt.md`](docs/master_prompt.md)
- Design: [`docs/design/`](docs/design/) (Parts 1A–1H; ERD in Part 1F; generation, loader, CDC and anomalies in Part 1G)
- Status and decisions log: [`docs/roadmap.md`](docs/roadmap.md), [`CLAUDE.md`](CLAUDE.md) (decision index D1–D92)

## Tech stack

Python 3.11+, MySQL 8.0.16+ (native install), DuckDB, SQLAlchemy / PyMySQL, PyArrow, Faker, YAML config, Typer CLI, Streamlit.

## Setup

1. **MySQL** 8.0.16 or newer, installed natively and running (e.g. `brew install mysql && brew services start mysql`).
2. **Python environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"          # or: pip install -r requirements.txt && pip install -e . --no-deps
   ```
3. **Secrets:** copy `.env.example` to `.env` and set the MySQL admin login (a fresh Homebrew install uses `root` with an empty password). `.env` is gitignored.
4. **Check the server, then create the service accounts and control schema** (safe to re-run):
   ```bash
   scmgen db check                  # every line [PASS]
   scmgen db init                   # creates scm_app, scm_reader, scm_anomaly; passwords saved to .env
   scmgen db check --as app         # also: --as reader, --as anomaly
   ```

## Quick start (a few minutes on the Small preset)

```bash
scmgen workspace create --code demo --company "Kaveri Nexus Distribution Pvt. Ltd." --industries electronics,fmcg --seed 42
scmgen generate history -w demo --preset small --set history.start_date=2025-04-01 --set history.end_date=2025-09-30
scmgen load -w demo                    # MySQL → data/duckdb/demo.duckdb
scmgen ui                              # control panel on http://localhost:8501
```

Then query the replica from Python (`duckdb.connect("data/duckdb/demo.duckdb").sql("SELECT COUNT(*) FROM raw.orders")`) or the DuckDB CLI if installed, or use the control panel's *Data explorer* and *Table guide*.

## Workspaces

One workspace per fictional company: `scm_<code>_oltp` (clean data), `scm_<code>_sim` (simulation copies), `data/duckdb/<code>.duckdb` and `<code>_sim.duckdb`.

```bash
scmgen workspace list
scmgen workspace show -w demo
scmgen workspace delete -w demo              # asks you to type the code; --purge frees the code for reuse
```

## Generation

Settings live in `config/generation.yaml` (presets `small` / `medium` / `large`); `--set` overrides any of them for one run, and a mistyped name is refused. Industry packs are in `config/industries/`, reference data in `config/seed/`.

```bash
scmgen generate config --preset small                    # the effective settings
scmgen generate packs --industries beauty,electronics    # check a pack combination
scmgen generate history -w demo --preset small           # master data, then day by day to the end date
scmgen generate history -w demo --days 30                # only the next 30 days
scmgen generate history -w demo --scenario stress        # a scenario profile of config/scenarios.yaml
```

Re-run actions (D14):

```bash
scmgen generate plan -w demo                 # what Continue would do, without writing
scmgen generate continue -w demo             # same settings, up to yesterday (--days N, --allow-future)
scmgen generate rebuild -w demo --confirm demo   # drop and regenerate: same seed ⇒ the same data
```

Every run ends with validation. A failure marks the workspace `NEEDS_ATTENTION`, which blocks Continue and loading until it passes (or `--force`).

## Validation and scenarios

```bash
scmgen validate -w demo                  # all 101 rules; writes data/reports/demo/validation_<time>.json
scmgen validate -w demo --check V-IN-01  # one rule (or --module IN); --all also lists passed rules
scmgen report scenarios -w demo          # the 20 business scenarios of the master prompt vs their target bands
```

## DuckDB loader

```bash
scmgen load -w demo                      # first load: full; then incremental (append / merge by watermark)
scmgen load -w demo --full               # reload everything
scmgen load -w demo --table orders       # reload one table
scmgen load status -w demo               # watermarks, last load per table, errors
```

Strategies per table are in `config/loader/tables.yaml`. Each batch commits together with its watermark, so a crashed load resumes and a re-run moves nothing twice; every load ends with a reconciliation against MySQL. Load metadata is in the replica's `etl_meta` schema (`etl_load_history`, `etl_table_watermarks`, `etl_error_log`).

## Control panel

```bash
scmgen ui            # --port 8501
```

Pages: Workspaces (create, generate, continue, rebuild), Run monitor (live progress, cancel), Data explorer, Validation, Scenarios, DuckDB load, Simulation, Table guide, Reset. Actions start the same CLI commands as background jobs (logs in `logs/ui_jobs/`), so closing the browser doesn't stop a run.

## CDC and anomaly simulation (Phase 7)

Everything runs on `scm_<code>_sim` and `<code>_sim.duckdb`; the clean data is never written (the anomaly tooling's MySQL user can only read it).

```bash
scmgen simulate reset -w demo                       # copy the clean tables into scm_demo_sim (no constraints)
scmgen load -w demo --source sim                    # replica of the copies
scmgen simulate cdc capture -w demo                 # change events since the last capture (first: snapshot)
scmgen simulate cdc apply -w demo                   # → cdc_current + SCD2 cdc_history, convergence check
scmgen simulate scenarios                           # data_quality, business_rules, pipeline, cdc_stream
scmgen simulate inject -w demo --scenario data_quality --seed 1
scmgen simulate report -w demo                      # caught / missed / unexplained per anomaly
scmgen simulate status -w demo
```

Set `cdc.enabled: true` in `config/simulation.yaml` to capture automatically after every generation run.

## Tests

```bash
pytest                          # everything (~25 minutes; needs MySQL)
pytest -m "not integration"     # unit tests only (no MySQL needed)
pytest -m integration           # tests against the local MySQL in .env
scmgen db check-design          # the schema matches the design docs (0 differences)
```

Logs are written as JSON lines to `logs/scmgen.log` (gitignored).
