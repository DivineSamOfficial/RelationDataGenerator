"""The generic MySQL → DuckDB loader (1G §6; D69-D71; master prompt §9; Phase 4).

One code path for every table, driven by ``config/loader/tables.yaml`` and ``information_schema``:

* **FULL** — the source table is streamed (server-side cursor, Arrow batches) into ``raw._stage_<t>``, then in one
  DuckDB transaction the old table is dropped and the stage renamed: readers never see a half-loaded table. First
  loads of incremental tables, ``--full``, a table reload and a changed source build (A2) use it too; it sets the
  table's watermark to the last (timestamp, PK) loaded.
* **APPEND / MERGE** — keyset batches after the watermark: ``(ts, pk) > (last_ts, last_pk) AND ts <= high``, ordered
  by (ts, pk), ``batch_size`` rows; each batch is inserted if absent (APPEND, by PK) or upserted (MERGE) and the
  watermark moves to its last row **in the same DuckDB transaction** (D71), so a crash resumes from the last good
  batch and a re-run of the same window changes nothing. ``high`` = ``MAX(generation_runs.committed_through)``: rows
  of a day still being generated are never read (D67).
* **Safety net** — after an APPEND load, source rows updated after insert (``updated_at > created_at``) are logged
  as ``STRATEGY_VIOLATION`` (V-LD-02).
* **Reconciliation** after each table (1G §6.6, V-LD-01): row count and distinct PKs, Σ of every DECIMAL column and
  the max watermark against the source within the high-water mark; sums only while no generation run is in
  progress (otherwise SKIPPED).
* A batch that fails is retried (``retries``, with backoff); after that the table is FAILED, the others go on and
  the load is PARTIAL. Everything is recorded in ``etl_meta`` (history, watermarks, errors).
* **Schema drift** — before an incremental load the source columns are compared with the replica's; a difference
  (column added, dropped or retyped) is logged as ``SCHEMA_DRIFT`` and the table is reloaded in full with the new
  shape (D92).
* **Sources** (D70): ``oltp`` (default) loads ``scm_<code>_oltp`` into ``<code>.duckdb``; ``sim`` loads the staging
  copies of ``scm_<code>_sim`` (Phase 7) into ``<code>_sim.duckdb``, keyed on the copy's ``sim_build_id`` so a
  ``simulate reset`` triggers a full reload. The validation gate (D76) applies to the clean source only.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import wall_clock
from scmgen.core.errors import ScmgenError, WorkspaceError
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import get_workspace
from scmgen.loader import meta
from scmgen.loader.config import LoaderConfig, load_loader_config
from scmgen.loader.replica import connect_replica
from scmgen.loader.schema import TableSchema, discover, to_arrow


class LoaderError(ScmgenError):
    """A load that can't start (gate, missing source) or a table that failed."""


@dataclass
class TableLoad:
    table: str
    strategy: str
    load_type: str
    rows_extracted: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    batches: int = 0
    status: str = "RUNNING"
    reconciliation: str = "SKIPPED"
    message: str | None = None
    seconds: float = 0.0


@dataclass
class LoadResult:
    workspace: str
    duckdb_path: Path
    load_batch_id: str
    high_water_mark: datetime | None
    full_reason: str | None
    tables: list[TableLoad] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.tables:
            return "NOTHING_TO_DO"
        failed = sum(t.status == "FAILED" for t in self.tables)
        return "SUCCESS" if not failed else ("FAILED" if failed == len(self.tables) else "PARTIAL")

    @property
    def reconciled(self) -> bool:
        return all(t.reconciliation != "FAILED" for t in self.tables)


def _quote(name: str) -> str:
    return f'"{name}"'


def _create(con: duckdb.DuckDBPyConnection, name: str, schema: TableSchema) -> None:
    columns = ", ".join(f"{_quote(c.name)} {c.duckdb_type}" for c in schema.columns)
    con.execute(f"CREATE TABLE raw.{_quote(name)} ({columns}, PRIMARY KEY ({_quote(schema.primary_key)}))")


SOURCES = ("oltp", "sim")


def replica_path(workspace, source: str = "oltp") -> Path:
    path = Path(workspace.duckdb_path)
    return path if source == "oltp" else path.with_name(f"{workspace.code}_sim.duckdb")


class Loader:
    def __init__(self, settings: Settings, code: str, config: LoaderConfig | None = None, source: str = "oltp") -> None:
        if source not in SOURCES:
            raise LoaderError(f"Unknown source {source!r}: use one of {SOURCES}")
        self.settings = settings
        self.source = source
        self.workspace = get_workspace(settings, code)
        self.config = config or load_loader_config()
        self.path = replica_path(self.workspace, source)
        self.source_schema = self.workspace.oltp_schema if source == "oltp" else self.workspace.sim_schema
        self.build_id = self.workspace.build_id

    # ------------------------------------------------------------------------------------------ public

    def load(self, full: bool = False, tables: list[str] | None = None, force: bool = False) -> LoadResult:
        workspace = self.workspace
        if self.source == "oltp" and workspace.status == "NEEDS_ATTENTION" and not force:
            raise LoaderError(f"Workspace '{workspace.code}' failed its last validation (D76): fix it or load with --force")
        unknown = sorted(set(tables or []) - set(self.config.strategies))
        if unknown:
            raise LoaderError(f"Unknown table(s) {unknown}: see config/loader/tables.yaml")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_mysql_engine(self.settings, role="app", database=self.source_schema)
        con = connect_replica(self.path)
        try:
            con.execute(f"SET threads = {self.config.duckdb_threads}")
            meta.ensure(con)
            with engine.connect() as source:
                if self.source == "sim":
                    self.build_id = self._sim_build(source)
                high = source.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
                running = source.execute(text("SELECT COUNT(*) FROM generation_runs WHERE run_status = 'RUNNING'")).scalar()
                schemas = discover(source)
                missing = sorted(set(self.config.strategies) - set(schemas))
                if missing:
                    raise LoaderError(f"Tables in loader config but not in the source: {missing}")
                replica_build = meta.info(con, "source_build_id")
                full_reason = None
                if replica_build != self.build_id:
                    full_reason = ("first load" if replica_build is None else
                                   f"source rebuilt (build {replica_build} → {self.build_id})")
                    full, tables = True, None
                elif full:
                    full_reason = "--full requested"
                result = LoadResult(workspace.code, self.path, uuid.uuid4().hex[:12], high, full_reason)
                if high is None:
                    return result
                for table in sorted(tables or self.config.strategies):
                    result.tables.append(self._load_table(con, source, schemas[table], table, full or bool(tables),
                                                          high, running == 0, result.load_batch_id,
                                                          single=bool(tables)))
                meta.set_info(con, "source_build_id", self.build_id, wall_clock())
                meta.set_info(con, "source", self.source, wall_clock())
                meta.set_info(con, "workspace", workspace.code, wall_clock())
                meta.set_info(con, "high_water_mark", high.isoformat(), wall_clock())
            return result
        finally:
            con.close()
            engine.dispose()

    # ------------------------------------------------------------------------------------------ one table

    def _load_table(self, con, source: Connection, schema: TableSchema, table: str, full: bool, high: datetime,
                    quiescent: bool, batch_id: str, single: bool = False) -> TableLoad:
        strategy = self.config.strategies[table]
        mark = meta.watermark(con, table)
        drift = None if mark is None else self._drift(con, schema, table)
        reload = full or strategy == "FULL" or mark is None or drift is not None
        load_type = ("TABLE_RELOAD" if single else
                     "FULL" if reload else f"INCREMENTAL_{strategy}")
        outcome = TableLoad(table, strategy, load_type)
        load_id = meta.next_id(con, "etl_load_history", "load_id")
        started = wall_clock()
        con.execute("""INSERT INTO etl_meta.etl_load_history (load_id, load_batch_id, source_schema, source_table,
                           target_table, load_type, source_build_id, watermark_from_ts, watermark_from_pk,
                           high_water_mark, started_at, load_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING')""",
                    [load_id, batch_id, self.source_schema, table, f"raw.{table}", load_type,
                     self.build_id, None if reload else mark["ts"], None if reload else mark["pk"], high,
                     started])
        clock = time.perf_counter()
        if drift is not None:
            outcome.message = f"schema drift: {drift}; reloaded in full"
            meta.log_error(con, load_id, table, None, "SCHEMA", "SCHEMA_DRIFT", drift, wall_clock())
        try:
            if reload:
                self._full(con, source, schema, table, strategy, high, load_id, outcome)
            else:
                self._incremental(con, source, schema, table, strategy, mark, high, load_id, outcome)
            if strategy == "APPEND":
                self._safety_net(con, source, table, load_id)
            outcome.status = "SUCCESS"
            outcome.reconciliation = self._reconcile(con, source, schema, table, strategy, high, quiescent, load_id)
        except Exception as exc:                     # the table fails, the load goes on (PARTIAL)
            outcome.status, outcome.message = "FAILED", f"{type(exc).__name__}: {exc}"[:500]
            meta.log_error(con, load_id, table, outcome.batches, "MERGE", "LOAD_FAILED", outcome.message, wall_clock())
        outcome.seconds = round(time.perf_counter() - clock, 2)
        mark = meta.watermark(con, table)
        con.execute("""UPDATE etl_meta.etl_load_history SET finished_at = ?, batches = ?, rows_extracted = ?,
                           rows_inserted = ?, rows_updated = ?, load_status = ?, reconciliation_status = ?,
                           error_message = ?, watermark_to_ts = ?, watermark_to_pk = ? WHERE load_id = ?""",
                    [wall_clock(), outcome.batches, outcome.rows_extracted, outcome.rows_inserted, outcome.rows_updated,
                     outcome.status if outcome.status == "SUCCESS" or not outcome.batches else "PARTIAL"
                     if outcome.status == "FAILED" and load_type.startswith("INCREMENTAL") else outcome.status,
                     outcome.reconciliation, outcome.message, mark["ts"] if mark else None, mark["pk"] if mark else None,
                     load_id])
        return outcome

    def _column_list(self, schema: TableSchema) -> str:
        return ", ".join(f"`{c}`" for c in schema.names)

    def _full(self, con, source: Connection, schema: TableSchema, table: str, strategy: str, high: datetime,
              load_id: int, outcome: TableLoad) -> None:
        column = self.config.watermark_column(table)
        stage = f"_stage_{table}"
        con.execute(f"DROP TABLE IF EXISTS raw.{_quote(stage)}")
        _create(con, stage, schema)
        where = f"WHERE `{column}` <= :high" if column else ""
        last: tuple[Any, Any] | None = None
        ts_index = schema.names.index(column) if column else None
        pk_index = schema.names.index(schema.primary_key)
        result = source.execution_options(stream_results=True, max_row_buffer=self.config.batch_size).execute(
            text(f"SELECT {self._column_list(schema)} FROM `{table}` {where} ORDER BY `{schema.primary_key}`"),
            {"high": high})
        while True:
            rows = result.fetchmany(self.config.batch_size)
            if not rows:
                break
            batch = to_arrow(schema, rows)
            con.register("batch", batch)
            con.execute(f"INSERT INTO raw.{_quote(stage)} SELECT * FROM batch")
            con.unregister("batch")
            outcome.batches += 1
            outcome.rows_extracted += len(rows)
            if ts_index is not None:
                for row in rows:
                    key = (row[ts_index], row[pk_index])
                    if last is None or key > last:
                        last = key
        result.close()
        outcome.rows_inserted = outcome.rows_extracted
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"DROP TABLE IF EXISTS raw.{_quote(table)}")
            con.execute(f"ALTER TABLE raw.{_quote(stage)} RENAME TO {_quote(table)}")
            meta.set_watermark(con, table, strategy, column, last[0] if last else None, last[1] if last else None,
                               self.build_id, load_id, wall_clock())
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    def _incremental(self, con, source: Connection, schema: TableSchema, table: str, strategy: str,
                     mark: dict[str, Any], high: datetime, load_id: int, outcome: TableLoad) -> None:
        column = self.config.watermark_column(table)
        pk = schema.primary_key
        ts_index, pk_index = schema.names.index(column), schema.names.index(pk)
        last_ts, last_pk = mark["ts"], mark["pk"]
        while True:
            after = ("" if last_ts is None else
                     f"AND (`{column}` > :ts OR (`{column}` = :ts AND `{pk}` > :pk))")
            rows = self._attempt(lambda: source.execute(text(
                f"SELECT {self._column_list(schema)} FROM `{table}` WHERE `{column}` <= :high {after} "
                f"ORDER BY `{column}`, `{pk}` LIMIT {self.config.batch_size}"),
                {"high": high, "ts": last_ts, "pk": last_pk}).fetchall(), con, load_id, table, outcome)
            if not rows:
                break
            batch = to_arrow(schema, rows)
            new_ts, new_pk = rows[-1][ts_index], rows[-1][pk_index]

            def apply() -> tuple[int, int]:
                con.execute("BEGIN TRANSACTION")
                try:
                    con.register("batch", batch)
                    existing = con.execute(f"SELECT COUNT(*) FROM raw.{_quote(table)} t JOIN batch b "
                                           f"ON t.{_quote(pk)} = b.{_quote(pk)}").fetchone()[0]
                    if strategy == "APPEND":
                        con.execute(f"INSERT INTO raw.{_quote(table)} SELECT b.* FROM batch b WHERE NOT EXISTS "
                                    f"(SELECT 1 FROM raw.{_quote(table)} t WHERE t.{_quote(pk)} = b.{_quote(pk)})")
                        counts = (len(rows) - existing, 0)
                    else:
                        con.execute(f"INSERT OR REPLACE INTO raw.{_quote(table)} SELECT * FROM batch")
                        counts = (len(rows) - existing, existing)
                    con.unregister("batch")
                    meta.set_watermark(con, table, strategy, column, new_ts, new_pk, self.build_id, load_id,
                                       wall_clock())
                    con.execute("COMMIT")
                    return counts
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            inserted, updated = self._attempt(apply, con, load_id, table, outcome)
            outcome.batches += 1
            outcome.rows_extracted += len(rows)
            outcome.rows_inserted += inserted
            outcome.rows_updated += updated
            last_ts, last_pk = new_ts, new_pk
            if len(rows) < self.config.batch_size:
                break

    def _attempt(self, action, con, load_id: int, table: str, outcome: TableLoad):
        """Run a batch step with retries and backoff; errors go to etl_error_log."""
        for attempt in range(self.config.retries + 1):
            try:
                return action()
            except Exception as exc:
                meta.log_error(con, load_id, table, outcome.batches + 1, "EXTRACT", type(exc).__name__, str(exc),
                               wall_clock(), retry=attempt)
                if attempt == self.config.retries:
                    raise
                time.sleep(min(2 ** attempt, 10) * 0.1)

    # ------------------------------------------------------------------------------------------ checks

    def _sim_build(self, source: Connection) -> str:
        try:
            build = source.execute(text("SELECT info_value FROM sim_copy_info WHERE info_key = 'sim_build_id'")).scalar()
        except Exception:
            build = None
        if not build:
            raise LoaderError(f"No staging copies in {self.source_schema}: run `scmgen simulate reset -w "
                              f"{self.workspace.code}` first")
        return build

    def _drift(self, con, schema: TableSchema, table: str) -> str | None:
        """Source columns vs the replica table's (name and type): None when they agree."""
        replica = con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'raw' "
                              "AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()
        if not replica:
            return None
        have = {name: kind for name, kind in replica}
        want = {c.name: c.duckdb_type for c in schema.columns}
        added, dropped = sorted(set(want) - set(have)), sorted(set(have) - set(want))
        retyped = sorted(c for c in set(want) & set(have) if want[c] != have[c] and not (
            want[c] == "JSON" and have[c] == "JSON"))
        parts = ([f"added {added}"] if added else []) + ([f"dropped {dropped}"] if dropped else []) + \
            ([f"retyped {retyped}"] if retyped else [])
        return ", ".join(parts) or None

    def _safety_net(self, con, source: Connection, table: str, load_id: int) -> None:
        changed = source.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE updated_at > created_at")).scalar()
        if changed:
            meta.log_error(con, load_id, table, None, "RECONCILE", "STRATEGY_VIOLATION",
                           f"{changed} source row(s) updated after insert: reclassify {table} as MERGE", wall_clock())

    def _reconcile(self, con, source: Connection, schema: TableSchema, table: str, strategy: str, high: datetime,
                   quiescent: bool, load_id: int) -> str:
        """Count, distinct PKs, Σ DECIMAL columns and the max watermark against the source (V-LD-01)."""
        decimals = [c.name for c in schema.columns if c.duckdb_type.startswith("DECIMAL")]
        sums_src = ", ".join(f"SUM(`{c}`)" for c in decimals)
        created = "WHERE created_at <= :high" if strategy != "FULL" else ""
        source_row = source.execute(text(
            f"SELECT COUNT(*), COUNT(DISTINCT `{schema.primary_key}`){', ' + sums_src if decimals else ''} "
            f"FROM `{table}` {created}"), {"high": high}).fetchone()
        sums_dst = ", ".join(f"SUM({_quote(c)})" for c in decimals)
        target_row = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {_quote(schema.primary_key)}){', ' + sums_dst if decimals else ''} "
            f"FROM raw.{_quote(table)}").fetchone()
        problems = []
        if source_row[0] != target_row[0] or target_row[1] != target_row[0]:
            problems.append(f"rows {target_row[0]} (distinct PKs {target_row[1]}) vs source {source_row[0]}")
        if quiescent:
            for name, a, b in zip(decimals, source_row[2:], target_row[2:]):
                if (Decimal(a or 0)) != (Decimal(b or 0)):
                    problems.append(f"Σ {name} {b} vs source {a}")
        if problems:
            meta.log_error(con, load_id, table, None, "RECONCILE", "RECONCILIATION_MISMATCH", "; ".join(problems)[:2000],
                           wall_clock())
            return "FAILED"
        return "PASSED" if quiescent else "SKIPPED"


def load_workspace(settings: Settings, code: str, full: bool = False, tables: list[str] | None = None,
                   force: bool = False, source: str = "oltp") -> LoadResult:
    return Loader(settings, code, source=source).load(full=full, tables=tables, force=force)


def load_status(settings: Settings, code: str, source: str = "oltp") -> dict[str, Any]:
    """Watermarks, the last load per table and recent errors of a workspace's replica."""
    workspace = get_workspace(settings, code)
    path = replica_path(workspace, source)
    if not path.exists() or (source == "sim" and not _has_meta(path)):
        flag = "" if source == "oltp" else " --source sim"
        raise WorkspaceError(f"No DuckDB replica yet for '{code}' ({path}): run `scmgen load -w {code}{flag}`")
    con = connect_replica(path, read_only=True)
    try:
        info = dict(con.execute("SELECT info_key, info_value FROM etl_meta.replica_info").fetchall())
        marks = con.execute("""SELECT w.source_table, w.strategy, w.last_watermark_ts, w.last_watermark_pk,
                                      w.target_row_count, h.load_type, h.load_status, h.reconciliation_status,
                                      h.finished_at
                               FROM etl_meta.etl_table_watermarks w
                               LEFT JOIN etl_meta.etl_load_history h ON h.load_id = w.last_load_id
                               ORDER BY w.source_table""").fetchall()
        errors = con.execute("""SELECT occurred_at, source_table, error_stage, error_type, error_message
                                FROM etl_meta.etl_error_log ORDER BY error_id DESC LIMIT 20""").fetchall()
    finally:
        con.close()
    build = workspace.build_id if source == "oltp" else _sim_build_id(settings, workspace)
    return {"path": path, "info": info, "tables": marks, "errors": errors,
            "build_matches": info.get("source_build_id") == build}


def _has_meta(path: Path) -> bool:
    con = connect_replica(path, read_only=True)
    try:
        return bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'etl_meta' "
                                "AND table_name = 'replica_info'").fetchone()[0])
    finally:
        con.close()


def _sim_build_id(settings: Settings, workspace) -> str | None:
    engine = create_mysql_engine(settings, role="app", database=workspace.sim_schema)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT info_value FROM sim_copy_info WHERE info_key = 'sim_build_id'")).scalar()
    except Exception:
        return None
    finally:
        engine.dispose()
