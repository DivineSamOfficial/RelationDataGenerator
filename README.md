# RelationDataGenerator

A synthetic data platform that simulates the operational database (OLTP) of a fictional Indian supply chain and trading company — procurement, inventory, sales, shipping, returns, expenses, GST and double-entry accounting — and replicates it into DuckDB for data engineering and analytics practice.

All company, person and tax identifiers it generates are fictional.

## Status

Phase 1 (architecture and design) is complete and approved. Phase 2 (MySQL schema) is in progress. See [`docs/roadmap.md`](docs/roadmap.md).

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

## Tests

```bash
pytest                     # all tests
pytest -m "not integration"   # unit tests only (no MySQL needed)
pytest -m integration      # tests against the local MySQL in .env
```

Logs are written as JSON lines to `logs/scmgen.log` (gitignored).
