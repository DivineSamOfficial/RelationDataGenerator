# RelationDataGenerator

A synthetic data platform that simulates the operational database (OLTP) of a fictional Indian supply chain and trading company — procurement, inventory, sales, shipping, returns, expenses, GST and double-entry accounting — and replicates it into DuckDB for data engineering and analytics practice.

All company, person and tax identifiers it generates are fictional.

## Status

Phases 1 (design) and 2 (MySQL schema) are complete. Phase 3 (data generation engine) is in progress. See [`docs/roadmap.md`](docs/roadmap.md).

## Documentation

- Requirements: [`docs/master_prompt.md`](docs/master_prompt.md)
- Design: [`docs/design/`](docs/design/) (Parts 1A–1H; ERD in Part 1F)

## Tech stack

Python 3.11+, MySQL 8.0.16+, DuckDB, SQLAlchemy/PyMySQL, Faker, YAML config, Typer CLI, Streamlit (control panel).

## Setup

1. **MySQL** 8.0.16 or newer, installed natively and running (e.g. `brew install mysql && brew services start mysql`).
2. **Python environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"          # or: pip install -r requirements.txt && pip install -e . --no-deps
   ```
3. **Secrets:** copy `.env.example` to `.env` and set the MySQL admin login (a fresh Homebrew install uses `root` with an empty password). `.env` is gitignored.
4. **Check the server:**
   ```bash
   scmgen db check
   ```
   Expected: every line `[PASS]`, exit code 0. Exit code 1 means a blocking requirement failed (e.g. MySQL too old or strict mode off); 2 means a configuration or connection problem.
5. **Create the service accounts and control schema** (safe to re-run):
   ```bash
   scmgen db init
   scmgen db check --as app        # also: --as reader, --as anomaly
   ```
   `db init` creates `scm_app` (all work, schemas `scm_*`), `scm_reader` (read-only) and `scm_anomaly` (read-only on clean `*_oltp` schemas, write on `*_sim`). Their passwords are generated and saved to `.env`. Re-running re-applies those passwords and grants.
6. **Create a workspace** (one per fictional company):
   ```bash
   scmgen workspace create --code kaveri --company "Kaveri Nexus Distribution Pvt. Ltd." --industries electronics,home_kitchen --seed 42
   scmgen workspace list
   scmgen workspace show -w kaveri
   scmgen workspace delete -w kaveri            # asks you to type the code; --purge frees the code for reuse
   ```
   `create` builds `scm_<code>_oltp` with all 92 tables (empty until data generation, Phase 3) and an empty `scm_<code>_sim`.
7. **Check the schema against the design docs** (run after any DDL or design change):
   ```bash
   scmgen db check-design               # builds the DDL in a scratch schema, compares, drops it
   scmgen db check-design -w kaveri     # checks a live workspace for drift
   ```
   Lists every difference (missing/extra table or column, type, NULL, generated, PK, FK target) and exits with code 1 if there is any.

## Generation (Phase 3, in progress)

```bash
scmgen generate config                                  # effective settings of the default (medium) preset
scmgen generate config --preset small --set volumes.customers_b2b=500 --set history.start_date=2026-04-01
```
Settings live in `config/generation.yaml`; `--set` overrides any of them for one run, and a mistyped name is refused.

```bash
scmgen generate packs                                   # check seed data + every industry pack, summarise them
scmgen generate packs --industries beauty,electronics   # check a combination for one workspace
```
Reference data lives in `config/seed/`, industry packs in `config/industries/` (`electronics.yaml` documents the format).

## Validation

```bash
scmgen validate -w kaveri                 # all rules; writes data/reports/kaveri/validation_<time>.json
scmgen validate -w kaveri --check V-MD-02 # one rule (or --module MD); --all also lists passed / pending rules
```
A failed validation marks the workspace `NEEDS_ATTENTION`, which blocks Continue and loading until it passes again (D76).

## Tests

```bash
pytest                     # all tests
pytest -m "not integration"   # unit tests only (no MySQL needed)
pytest -m integration      # tests against the local MySQL in .env
```

Logs are written as JSON lines to `logs/scmgen.log` (gitignored).
