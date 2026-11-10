"""Deterministic surrogate IDs (D20).

The engine assigns every primary key itself, in the order it builds rows, instead of letting
``AUTO_INCREMENT`` choose. So a parent and its children can be built in memory and batch-inserted
without reading IDs back, and the same seed gives the same IDs (Rebuild is reproducible down to keys).

``AUTO_INCREMENT`` stays on the columns for manual inserts; explicit values move its counter on (A7).
A rolled-back day must also roll back the allocator (``checkpoint`` / ``restore``), otherwise the next
attempt would skip IDs and stop matching an uninterrupted run.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.engine import Connection

from scmgen.generation.schema_info import PrimaryKey, primary_keys


class IdAllocationError(RuntimeError):
    """Unknown table, or a PK type would overflow."""


class IdAllocator:
    def __init__(self, keys: Mapping[str, PrimaryKey], last_ids: Mapping[str, int]) -> None:
        self._keys = dict(keys)
        self._last = {table: int(last_ids.get(table, 0)) for table in keys}

    @classmethod
    def from_database(cls, conn: Connection, schema: str) -> IdAllocator:
        """Continue after the highest existing ID of every table (0 for empty tables)."""
        keys = primary_keys(conn, schema)
        last: dict[str, int] = {}
        for table, key in keys.items():
            # Identifiers come from information_schema, not user input.
            value = conn.exec_driver_sql(f"SELECT COALESCE(MAX(`{key.column}`), 0) FROM `{schema}`.`{table}`").scalar_one()
            last[table] = int(value)
        return cls(keys, last)

    def pk_column(self, table: str) -> str:
        return self._key(table).column

    def next(self, table: str) -> int:
        return self.block(table, 1).start

    def block(self, table: str, count: int) -> range:
        """``count`` consecutive new IDs for ``table``."""
        key = self._key(table)
        if count < 0:
            raise IdAllocationError(f"Cannot allocate {count} ids")
        first = self._last[table] + 1
        last = self._last[table] + count
        if last > key.max_value:
            raise IdAllocationError(f"{table}.{key.column} would exceed its maximum {key.max_value}")
        self._last[table] = last
        return range(first, last + 1)

    def last(self, table: str) -> int:
        self._key(table)
        return self._last[table]

    def checkpoint(self) -> dict[str, int]:
        """State to return to if the current day is rolled back."""
        return dict(self._last)

    def restore(self, checkpoint: Mapping[str, int]) -> None:
        self._last = dict(checkpoint)

    def _key(self, table: str) -> PrimaryKey:
        try:
            return self._keys[table]
        except KeyError:
            raise IdAllocationError(f"Unknown table {table!r}") from None
