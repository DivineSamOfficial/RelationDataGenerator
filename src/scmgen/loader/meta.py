"""DuckDB load metadata in ``etl_meta`` (1G §6.5): load history, watermarks, error log, replica info."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

DDL = [
    "CREATE SCHEMA IF NOT EXISTS raw",
    "CREATE SCHEMA IF NOT EXISTS etl_meta",
    """CREATE TABLE IF NOT EXISTS etl_meta.replica_info (
           info_key VARCHAR PRIMARY KEY, info_value VARCHAR, updated_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS etl_meta.etl_load_history (
           load_id UBIGINT PRIMARY KEY, load_batch_id VARCHAR NOT NULL, source_schema VARCHAR NOT NULL,
           source_table VARCHAR NOT NULL, target_table VARCHAR NOT NULL, load_type VARCHAR NOT NULL,
           source_build_id VARCHAR, watermark_from_ts TIMESTAMP, watermark_from_pk UBIGINT,
           watermark_to_ts TIMESTAMP, watermark_to_pk UBIGINT, high_water_mark TIMESTAMP,
           started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP, batches UINTEGER DEFAULT 0,
           rows_extracted UBIGINT DEFAULT 0, rows_inserted UBIGINT DEFAULT 0, rows_updated UBIGINT DEFAULT 0,
           rows_failed UBIGINT DEFAULT 0, load_status VARCHAR NOT NULL, reconciliation_status VARCHAR,
           error_message VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS etl_meta.etl_table_watermarks (
           source_table VARCHAR PRIMARY KEY, strategy VARCHAR NOT NULL, watermark_column VARCHAR,
           last_watermark_ts TIMESTAMP, last_watermark_pk UBIGINT, source_build_id VARCHAR,
           last_load_id UBIGINT, last_success_at TIMESTAMP, target_row_count UBIGINT)""",
    """CREATE TABLE IF NOT EXISTS etl_meta.etl_error_log (
           error_id UBIGINT PRIMARY KEY, load_id UBIGINT, source_table VARCHAR, batch_number UINTEGER,
           error_stage VARCHAR NOT NULL, error_type VARCHAR NOT NULL, error_message VARCHAR,
           record_key VARCHAR, record_payload JSON, occurred_at TIMESTAMP NOT NULL, retry_count UTINYINT DEFAULT 0)""",
]


def ensure(con: duckdb.DuckDBPyConnection) -> None:
    for statement in DDL:
        con.execute(statement)


def info(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT info_value FROM etl_meta.replica_info WHERE info_key = ?", [key]).fetchone()
    return row[0] if row else None


def set_info(con: duckdb.DuckDBPyConnection, key: str, value: str, now: datetime) -> None:
    con.execute("INSERT OR REPLACE INTO etl_meta.replica_info VALUES (?, ?, ?)", [key, value, now])


def next_id(con: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
    return int(con.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM etl_meta.{table}").fetchone()[0])


def watermark(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, Any] | None:
    row = con.execute("""SELECT strategy, watermark_column, last_watermark_ts, last_watermark_pk, source_build_id,
                                target_row_count FROM etl_meta.etl_table_watermarks WHERE source_table = ?""",
                      [table]).fetchone()
    if row is None:
        return None
    return dict(zip(("strategy", "watermark_column", "ts", "pk", "build_id", "rows"), row))


def set_watermark(con: duckdb.DuckDBPyConnection, table: str, strategy: str, column: str | None, ts, pk,
                  build_id: str, load_id: int, now: datetime) -> None:
    rows = con.execute(f'SELECT COUNT(*) FROM raw."{table}"').fetchone()[0]
    con.execute("INSERT OR REPLACE INTO etl_meta.etl_table_watermarks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [table, strategy, column, ts, pk, build_id, load_id, now, rows])


def log_error(con: duckdb.DuckDBPyConnection, load_id: int, table: str, batch: int | None, stage: str, kind: str,
              message: str, now: datetime, retry: int = 0) -> None:
    con.execute("""INSERT INTO etl_meta.etl_error_log (error_id, load_id, source_table, batch_number, error_stage,
                                                       error_type, error_message, occurred_at, retry_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [next_id(con, "etl_error_log", "error_id"), load_id, table, batch, stage, kind, message[:2000], now,
                 retry])
