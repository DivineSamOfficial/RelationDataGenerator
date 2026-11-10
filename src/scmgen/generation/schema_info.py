"""What the engine needs to know about a workspace schema, read from ``information_schema``.

Read once per run: primary-key column and its largest value per table (for the ID allocator) and the
FK dependency order of tables (for the batch writer, so parents are inserted before children).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Largest value of each unsigned integer PK type.
_INT_MAX = {
    "tinyint unsigned": 2**8 - 1,
    "smallint unsigned": 2**16 - 1,
    "mediumint unsigned": 2**24 - 1,
    "int unsigned": 2**32 - 1,
    "bigint unsigned": 2**64 - 1,
}


# D86: the schema has two FK cycles between tables, each closed by one nullable "late link" (a row-level
# cycle never occurs). The writer inserts these columns as NULL and sets them by UPDATE after the
# inserts, so a table order exists:
#   stock_adjustment_line_items.inventory_batch_id: an OPENING / FOUND line creates its batch (the batch
#       points to the line); a loss line points to a batch that already exists.
#   customer_payments.settlement_bank_transaction_id: a clearing-method receipt is settled by a bank
#       line days later (D63); bank lines and refunds point back to payments.
LATE_LINK_FOREIGN_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("stock_adjustment_line_items", "inventory_batch_id"),
    ("customer_payments", "settlement_bank_transaction_id"),
})


class SchemaInfoError(RuntimeError):
    """The schema doesn't have the shape the engine relies on."""


@dataclass(frozen=True)
class PrimaryKey:
    table: str
    column: str
    max_value: int


def primary_keys(conn: Connection, schema: str) -> dict[str, PrimaryKey]:
    """Single-column unsigned-integer PK of every base table (all 92 follow this convention)."""
    rows = conn.execute(text(
        "SELECT k.TABLE_NAME, k.COLUMN_NAME, c.COLUMN_TYPE FROM information_schema.KEY_COLUMN_USAGE k "
        "JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA = k.TABLE_SCHEMA AND c.TABLE_NAME = k.TABLE_NAME "
        "  AND c.COLUMN_NAME = k.COLUMN_NAME "
        "JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = k.TABLE_SCHEMA AND t.TABLE_NAME = k.TABLE_NAME "
        "WHERE k.TABLE_SCHEMA = :s AND k.CONSTRAINT_NAME = 'PRIMARY' AND t.TABLE_TYPE = 'BASE TABLE'"),
        {"s": schema}).all()
    keys: dict[str, PrimaryKey] = {}
    for table, column, column_type in rows:
        if table in keys:
            raise SchemaInfoError(f"{table} has a composite primary key; the engine expects one id column")
        max_value = _INT_MAX.get(column_type.lower())
        if max_value is None:
            raise SchemaInfoError(f"{table}.{column} is {column_type}; the engine expects an unsigned integer PK")
        keys[table] = PrimaryKey(table, column, max_value)
    return keys


def dependency_order(conn: Connection, schema: str) -> list[str]:
    """Base tables ordered so every FK target comes before the tables that reference it.

    Self-references are ignored (rows of one table are inserted in the order they were built, so a
    parent row is built first), and so are the late-link FKs (D86). Any other cycle is an error.
    Ties are broken alphabetically, so the order is stable.
    """
    tables = sorted(t for (t,) in conn.execute(text(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :s AND TABLE_TYPE = 'BASE TABLE'"),
        {"s": schema}))
    parents: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent, columns in conn.execute(text(
            "SELECT TABLE_NAME, REFERENCED_TABLE_NAME, GROUP_CONCAT(COLUMN_NAME) FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL "
            "GROUP BY TABLE_NAME, REFERENCED_TABLE_NAME, CONSTRAINT_NAME"), {"s": schema}):
        late = all((child, column) in LATE_LINK_FOREIGN_KEYS for column in columns.split(","))
        if child != parent and child in parents and not late:
            parents[child].add(parent)

    order: list[str] = []
    placed: set[str] = set()
    remaining = list(tables)
    while remaining:
        ready = [t for t in remaining if parents[t] <= placed]
        if not ready:
            raise SchemaInfoError(f"Foreign-key cycle among: {', '.join(remaining)}")
        for table in ready:
            order.append(table)
            placed.add(table)
        remaining = [t for t in remaining if t not in placed]
    return order
