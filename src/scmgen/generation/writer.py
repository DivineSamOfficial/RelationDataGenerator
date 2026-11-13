"""Batched writes inside the day transaction (1G §2.5).

Business steps build rows in memory and hand them to the writer. ``flush()`` writes them with multi-row
``INSERT`` statements (``batch_size`` rows each), parent tables before child tables, then applies the
buffered updates. The writer never commits: the engine owns the transaction (one per day, D64).

Rules for callers:

* Every row of a table carries the same columns, all explicit (IDs from the allocator, ``created_at`` /
  ``updated_at`` from the simulated clock). Generated columns are never written.
* Rows of a table are inserted in the order they were added, so a self-referencing parent (an
  employee's manager, a reversed journal) must be added before its child.
* Updates run after all inserts of the flush, in the order they were added.
* Late-link FK columns (D86) are inserted as NULL and set by an UPDATE straight after the inserts,
  because their target table is ordered after them. Callers just fill them in normally.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.generation.schema_info import LATE_LINK_FOREIGN_KEYS, dependency_order, primary_keys


class WriterError(RuntimeError):
    """Rows handed to the writer can't be written as given."""


def _quote(identifier: str) -> str:
    if "`" in identifier:
        raise WriterError(f"Invalid identifier {identifier!r}")
    return f"`{identifier}`"


class BatchWriter:
    def __init__(self, table_order: Sequence[str], batch_size: int = 1000,
                 pk_columns: Mapping[str, str] | None = None,
                 late_links: frozenset[tuple[str, str]] = LATE_LINK_FOREIGN_KEYS) -> None:
        if batch_size < 1:
            raise WriterError("batch_size must be at least 1")
        self._rank = {table: index for index, table in enumerate(table_order)}
        self._batch_size = batch_size
        self._pk_columns = dict(pk_columns or {})
        self._late: dict[str, tuple[str, ...]] = {}
        for table, column in sorted(late_links):
            self._late[table] = self._late.get(table, ()) + (column,)
        self._inserts: dict[str, list[dict[str, Any]]] = {}
        self._columns: dict[str, tuple[str, ...]] = {}
        self._updates: list[tuple[str, str, Any, dict[str, Any]]] = []
        self.written: Counter[str] = Counter()     # rows inserted per table since creation

    @classmethod
    def for_schema(cls, conn: Connection, schema: str, batch_size: int = 1000) -> BatchWriter:
        """A writer for a workspace schema: table order and primary keys read from information_schema."""
        keys = primary_keys(conn, schema)
        return cls(dependency_order(conn, schema), batch_size, {t: k.column for t, k in keys.items()})

    # ----------------------------------------------------------------- buffering

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        if table not in self._rank:
            raise WriterError(f"Unknown table {table!r}")
        columns = tuple(row)
        expected = self._columns.setdefault(table, columns)
        if columns != expected:
            raise WriterError(f"{table}: row columns {sorted(columns)} differ from earlier rows {sorted(expected)}")
        self._inserts.setdefault(table, []).append(dict(row))

    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.insert(table, row)

    def update(self, table: str, pk_column: str, pk_value: Any, values: Mapping[str, Any]) -> None:
        if table not in self._rank:
            raise WriterError(f"Unknown table {table!r}")
        if not values:
            raise WriterError(f"{table}: update without values")
        if pk_column in values:
            raise WriterError(f"{table}: the primary key can't be updated")
        self._updates.append((table, pk_column, pk_value, dict(values)))

    @property
    def pending_rows(self) -> int:
        return sum(len(rows) for rows in self._inserts.values()) + len(self._updates)

    # ----------------------------------------------------------------- writing

    def flush(self, conn: Connection) -> dict[str, int]:
        """Write everything buffered. Returns rows inserted per table by this flush."""
        inserted: dict[str, int] = {}
        late_updates: list[tuple[str, str, Any, dict[str, Any]]] = []
        for table in sorted(self._inserts, key=self._rank.__getitem__):
            rows = self._inserts[table]
            columns = self._columns[table]
            late_columns = [c for c in self._late.get(table, ()) if c in columns]
            if late_columns:
                pk_column = self._pk_columns.get(table)
                if pk_column is None:
                    raise WriterError(f"{table}: primary key unknown, can't defer late-link columns {late_columns}")
                deferred = []
                for row in rows:
                    links = {c: row[c] for c in late_columns if row[c] is not None}
                    if links:
                        late_updates.append((table, pk_column, row[pk_column], links))
                        row = {**row, **{c: None for c in links}}
                    deferred.append(row)
                rows = deferred
            statement = text(
                f"INSERT INTO {_quote(table)} ({', '.join(_quote(c) for c in columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})"
            )
            for start in range(0, len(rows), self._batch_size):
                conn.execute(statement, rows[start:start + self._batch_size])
            inserted[table] = len(rows)
            self.written[table] += len(rows)

        self._updates[:0] = late_updates
        # Consecutive updates of the same shape share one executemany.
        index = 0
        while index < len(self._updates):
            table, pk_column, _, values = self._updates[index]
            shape = (table, pk_column, tuple(values))
            group = []
            while index < len(self._updates) and (
                    self._updates[index][0], self._updates[index][1], tuple(self._updates[index][3])) == shape:
                _, _, pk_value, row_values = self._updates[index]
                group.append({**row_values, "_pk": pk_value})
                index += 1
            # Several updates of one row in a group: the last one wins, as when they ran one by one.
            latest = {row["_pk"]: row for row in group}
            group = list(latest.values())
            columns = list(values)
            assignments = ", ".join(f"t.{_quote(c)} = v.{_quote(c)}" for c in columns)
            aliases = ", ".join(["_k", *(_quote(c) for c in columns)])
            for start in range(0, len(group), self._batch_size):
                batch = group[start:start + self._batch_size]
                # One statement per batch: UPDATE … JOIN a VALUES table (PyMySQL would send one UPDATE per
                # row; this is ~3x faster on the daily flush). Values are bound parameters, so exact.
                params: dict[str, Any] = {}
                rows_sql = []
                for n, row in enumerate(batch):
                    params[f"k{n}"] = row["_pk"]
                    for i, column in enumerate(columns):
                        params[f"v{n}_{i}"] = row[column]
                    rows_sql.append("ROW(" + ", ".join([f":k{n}", *(f":v{n}_{i}" for i in range(len(columns)))]) + ")")
                statement = text(f"UPDATE {_quote(table)} AS t JOIN (VALUES {', '.join(rows_sql)}) AS v ({aliases}) "
                                 f"ON t.{_quote(pk_column)} = v._k SET {assignments}")
                # SQLAlchemy's MySQL dialects connect with CLIENT_FOUND_ROWS, so rowcount counts rows
                # *matched* by the key (not only changed ones): fewer means an id that doesn't exist.
                matched = conn.execute(statement, params).rowcount
                if matched != len(batch):
                    raise WriterError(f"{table}: {len(batch) - matched} of {len(batch)} updated rows not found "
                                      f"by {pk_column}")
        self.discard()
        return inserted

    def discard(self) -> None:
        """Drop everything buffered (after a flush, or when the day is rolled back)."""
        self._inserts.clear()
        self._columns.clear()
        self._updates.clear()
