# RelationDataGenerator

A synthetic data platform that simulates the operational database (OLTP) of a fictional Indian supply chain and trading company — procurement, inventory, sales, shipping, returns, expenses, GST and double-entry accounting — and replicates it into DuckDB for data engineering and analytics practice.

All company, person and tax identifiers it generates are fictional.

## Status

Phase 1 (architecture and design) in progress. See [`docs/roadmap.md`](docs/roadmap.md).

## Documentation

- Requirements: [`docs/master_prompt.md`](docs/master_prompt.md)
- Design: [`docs/design/`](docs/design/)

## Tech stack

Python 3.11+, MySQL 8.0.16+, DuckDB, SQLAlchemy/PyMySQL, Faker, YAML config, Streamlit (control panel).

Setup and usage instructions will be added as each phase is implemented.
