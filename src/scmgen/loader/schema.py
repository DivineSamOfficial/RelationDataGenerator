"""Source schema discovery and type mapping MySQL → DuckDB / Arrow (1G §6.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from sqlalchemy import text
from sqlalchemy.engine import Connection

_INTEGERS = {"tinyint": ("TINYINT", "UTINYINT", pa.int8(), pa.uint8()),
             "smallint": ("SMALLINT", "USMALLINT", pa.int16(), pa.uint16()),
             "mediumint": ("INTEGER", "UINTEGER", pa.int32(), pa.uint32()),
             "int": ("INTEGER", "UINTEGER", pa.int32(), pa.uint32()),
             "bigint": ("BIGINT", "UBIGINT", pa.int64(), pa.uint64())}


@dataclass(frozen=True)
class Column:
    name: str
    duckdb_type: str
    arrow_type: Any
    is_bool: bool


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]
    primary_key: str

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]


def _map(data_type: str, column_type: str, precision: int | None, scale: int | None) -> tuple[str, Any, bool]:
    data_type, column_type = data_type.lower(), column_type.lower()
    if data_type == "tinyint" and column_type.startswith("tinyint(1)"):
        return "BOOLEAN", pa.bool_(), True
    if data_type in _INTEGERS:
        signed, unsigned, arrow_signed, arrow_unsigned = _INTEGERS[data_type]
        return (unsigned, arrow_unsigned, False) if "unsigned" in column_type else (signed, arrow_signed, False)
    if data_type == "decimal":
        return f"DECIMAL({precision},{scale})", pa.decimal128(precision, scale), False
    if data_type in ("char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"):
        return "VARCHAR", pa.string(), False
    if data_type == "json":
        return "JSON", pa.string(), False
    if data_type == "date":
        return "DATE", pa.date32(), False
    if data_type in ("datetime", "timestamp"):
        return "TIMESTAMP", pa.timestamp("us"), False     # naive IST, µs (D77)
    if data_type in ("double", "float"):
        return "DOUBLE", pa.float64(), False
    raise ValueError(f"No DuckDB mapping for MySQL type {column_type}")


def discover(conn: Connection, tables: list[str] | None = None) -> dict[str, TableSchema]:
    """Columns (ordinal order, DuckDB and Arrow types) and the single-column primary key of each base table."""
    columns: dict[str, list[Column]] = {}
    for row in conn.execute(text("""
            SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.COLUMN_TYPE, c.NUMERIC_PRECISION, c.NUMERIC_SCALE
            FROM information_schema.COLUMNS c JOIN information_schema.TABLES t
              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
            WHERE c.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE = 'BASE TABLE'
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION""")).mappings():
        duck, arrow, is_bool = _map(row["DATA_TYPE"], row["COLUMN_TYPE"], row["NUMERIC_PRECISION"], row["NUMERIC_SCALE"])
        columns.setdefault(row["TABLE_NAME"], []).append(Column(row["COLUMN_NAME"], duck, arrow, is_bool))
    keys: dict[str, list[str]] = {}
    for table, column in conn.execute(text("""
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY TABLE_NAME, ORDINAL_POSITION""")):
        keys.setdefault(table, []).append(column)
    found = {}
    for table, cols in columns.items():
        if tables is not None and table not in tables:
            continue
        pk = keys.get(table, [])
        if len(pk) != 1:
            raise ValueError(f"{table}: the loader needs a single-column primary key, found {pk}")
        found[table] = TableSchema(table, tuple(cols), pk[0])
    return found


def to_arrow(schema: TableSchema, rows: list[tuple]) -> pa.Table:
    """One batch of source rows as an Arrow table with the replica's types."""
    arrays = []
    for index, column in enumerate(schema.columns):
        values = [r[index] for r in rows]
        if column.is_bool:
            values = [None if v is None else bool(v) for v in values]
        elif column.duckdb_type in ("VARCHAR", "JSON"):
            values = [None if v is None else (v if isinstance(v, str) else str(v)) for v in values]
        arrays.append(pa.array(values, type=column.arrow_type))
    return pa.Table.from_arrays(arrays, names=schema.names)
