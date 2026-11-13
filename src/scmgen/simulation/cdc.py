"""CDC simulation (1G §8; D72, D89): capture change events from ``_oltp`` into ``_sim.change_event_log`` and a
log-based consumer that applies them to DuckDB with SCD Type 2 history.

**Capture** (``capture``) works like log mining between two points in time. For every captured table it reads the
rows whose ``updated_at`` moved past the table's keyset position ``(updated_at, PK)`` up to the high-water mark
``MAX(generation_runs.committed_through)`` (rows of a day still being generated are never read, D67):

* the first capture of a table emits one ``SNAPSHOT`` event per row (like Debezium's initial snapshot);
* later captures emit ``INSERT`` for a row with no earlier event and ``UPDATE`` otherwise, with the before-image
  taken from the row's previous event and the list of changed columns;
* events of one capture get their LSNs (``change_event_id``) in source time order across all tables.

Granularity is the capture: a row changed several times between two captures yields one event with its latest
image. Capturing after every run (``cdc.enabled``) and continuing day by day (``generate continue``) gives one
capture per simulated day, which is what continuous mode does (D89).

**Consumer** (``apply``) reads events after its last LSN (``etl_meta.cdc_offsets``) in LSN order and keeps
``cdc_current.<table>`` (latest state, upsert by PK) and ``cdc_history.<table>`` (one row per version with
``_valid_from`` / ``_valid_to`` / ``_is_current``). It rejects, and logs in ``etl_meta.cdc_rejected_events``,
a re-delivered version (``DUPLICATE``: same ``updated_at`` as the version it holds) and a version older than the
one it holds (``STALE``: an out-of-order event), so duplicates and reordering never corrupt the target. The batch
and the offset commit together, so a re-run is idempotent. ``compare`` checks that the CDC target converged to the
source (row count, PKs, Σ of DECIMAL columns, max ``updated_at``).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import wall_clock
from scmgen.core.errors import ScmgenError
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import WorkspaceInfo, get_workspace
from scmgen.loader.config import load_loader_config
from scmgen.loader.schema import TableSchema, discover, to_arrow
from scmgen.simulation.config import load_simulation_config
from scmgen.simulation.sim_schema import ensure_control


class CdcError(ScmgenError):
    """CDC can't run (nothing captured, no workspace data)."""


# ------------------------------------------------------------------------------------------ images


def _json_value(value: Any, is_bool: bool) -> Any:
    if value is None:
        return None
    if is_bool:
        return bool(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def image(schema: TableSchema, row: tuple) -> dict[str, Any]:
    return {c.name: _json_value(v, c.is_bool) for c, v in zip(schema.columns, row)}


def _python_value(value: Any, duckdb_type: str) -> Any:
    if value is None:
        return None
    if duckdb_type.startswith("DECIMAL"):
        return Decimal(str(value))
    if duckdb_type == "TIMESTAMP":
        return datetime.fromisoformat(value)
    if duckdb_type == "DATE":
        return date.fromisoformat(value)
    if duckdb_type == "BOOLEAN":
        return bool(value)
    if duckdb_type == "JSON":
        return value if isinstance(value, str) else json.dumps(value)
    return value


def image_row(schema: TableSchema, data: dict[str, Any]) -> tuple:
    """An image back as a source-typed row (columns added later are NULL, dropped ones ignored)."""
    return tuple(_python_value(data.get(c.name), c.duckdb_type) for c in schema.columns)


# ------------------------------------------------------------------------------------------ capture


def captured_tables(settings_tables: str | list[str], schemas: dict[str, TableSchema]) -> list[str]:
    if settings_tables == "all":
        return sorted(schemas)
    if settings_tables == "merge":
        strategies = load_loader_config().strategies
        return sorted(t for t, s in strategies.items() if s == "MERGE" and t in schemas)
    unknown = sorted(set(settings_tables) - set(schemas))
    if unknown:
        raise CdcError(f"cdc.tables lists unknown table(s) {unknown}")
    return sorted(settings_tables)


@dataclass
class CaptureResult:
    capture_batch_id: str
    high_water_mark: datetime | None
    events: dict[str, dict[str, int]] = field(default_factory=dict)    # table → operation → count
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(sum(ops.values()) for ops in self.events.values())


def _engines(settings: Settings, workspace: WorkspaceInfo):
    return (create_mysql_engine(settings, role="app", database=workspace.oltp_schema),
            create_mysql_engine(settings, role="app", database=workspace.sim_schema))


_INSERT_EVENT = text("""
    INSERT INTO change_event_log (source_table, operation, row_pk, primary_key, before_image, after_image,
        changed_columns, source_committed_at, capture_batch_id, generation_run_id, is_simulated_anomaly, anomaly_id,
        captured_at, created_at, updated_at)
    VALUES (:table, :op, :pk, :pk_json, :before, :after, :changed, :ts, :batch, :run, :anomaly, :anomaly_id,
            :now, :now, :now)""")


def _event(table: str, pk_name: str, pk: int, op: str, before: dict | None, after: dict | None, ts: datetime,
           batch: str, run: int | None, now: datetime, anomaly_id: int | None = None) -> dict[str, Any]:
    changed = None
    if op == "UPDATE":
        changed = json.dumps([k for k in after if before.get(k) != after[k]])
    return {"table": table, "op": op, "pk": pk, "pk_json": json.dumps({pk_name: pk}),
            "before": None if before is None else json.dumps(before), "after": None if after is None else json.dumps(after),
            "changed": changed, "ts": ts, "batch": batch, "run": run, "anomaly": anomaly_id is not None,
            "anomaly_id": anomaly_id, "now": now}


def last_images(sim: Connection, table: str, pks: list[int]) -> dict[int, dict[str, Any]]:
    """The latest genuine after-image of each row (the before-image of its next UPDATE)."""
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(pks), 1000):
        chunk = pks[start:start + 1000]
        rows = sim.execute(text(
            f"SELECT row_pk, after_image FROM change_event_log WHERE source_table = :t AND NOT is_simulated_anomaly "
            f"AND row_pk IN ({', '.join(str(int(p)) for p in chunk)}) ORDER BY change_event_id"), {"t": table})
        for pk, after in rows:
            found[int(pk)] = after if isinstance(after, dict) else json.loads(after) if after else None
    return found


def capture(settings: Settings, code: str, tables: list[str] | None = None) -> CaptureResult:
    """Capture the changes since the last capture into ``change_event_log`` (D89)."""
    clock = time.perf_counter()
    workspace = get_workspace(settings, code)
    config = load_simulation_config().cdc
    source_engine, sim_engine = _engines(settings, workspace)
    result = CaptureResult(uuid.uuid4().hex[:12], None)
    try:
        with source_engine.connect() as source, sim_engine.connect() as sim:
            ensure_control(sim)
            sim.commit()
            high = source.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
            run = source.execute(text("SELECT MAX(generation_run_id) FROM generation_runs")).scalar()
            result.high_water_mark = high
            if high is None:
                return result
            schemas = discover(source)
            selected = tables or captured_tables(config.tables, schemas)
            marks = {r[0]: r for r in sim.execute(text(
                "SELECT source_table, last_updated_at, last_pk, high_water_mark FROM cdc_capture_watermarks"))}
            pending: list[dict[str, Any]] = []
            now = wall_clock()
            for table in selected:
                schema = schemas[table]
                mark = marks.get(table)
                if mark is None:
                    result.events[table] = {"SNAPSHOT": _snapshot(source, sim, schema, high, result.capture_batch_id,
                                                                  run, now, config.batch_size)}
                    continue
                events, last = _changes(source, sim, schema, mark, high, result.capture_batch_id, run, now)
                pending.extend(events)
                counts: dict[str, int] = {}
                for event in events:
                    counts[event["op"]] = counts.get(event["op"], 0) + 1
                result.events[table] = counts
                _set_mark(sim, table, last or (mark[1], mark[2]), high, len(events), now)
            pending.sort(key=lambda e: (e["ts"], e["table"], e["pk"]))    # LSN order = source time order
            for start in range(0, len(pending), config.batch_size):
                sim.execute(_INSERT_EVENT, pending[start:start + config.batch_size])
            sim.commit()
    finally:
        source_engine.dispose()
        sim_engine.dispose()
    result.seconds = round(time.perf_counter() - clock, 2)
    return result


def _set_mark(sim: Connection, table: str, last: tuple, high: datetime, events: int, now: datetime) -> None:
    sim.execute(text("""
        INSERT INTO cdc_capture_watermarks (source_table, last_updated_at, last_pk, high_water_mark, captured_events,
                                            created_at, updated_at)
        VALUES (:t, :ts, :pk, :high, :n, :now, :now)
        ON DUPLICATE KEY UPDATE last_updated_at = VALUES(last_updated_at), last_pk = VALUES(last_pk),
            high_water_mark = VALUES(high_water_mark), captured_events = captured_events + VALUES(captured_events),
            updated_at = VALUES(updated_at)"""), {"t": table, "ts": last[0], "pk": last[1], "high": high, "n": events,
                                                 "now": now})


def _select(schema: TableSchema) -> str:
    return ", ".join(f"`{c}`" for c in schema.names)


def _snapshot(source: Connection, sim: Connection, schema: TableSchema, high: datetime, batch: str, run: int | None,
              now: datetime, batch_size: int) -> int:
    pk = schema.primary_key
    ts_index, pk_index = schema.names.index("updated_at"), schema.names.index(pk)
    result = source.execution_options(stream_results=True, max_row_buffer=batch_size).execute(text(
        f"SELECT {_select(schema)} FROM `{schema.name}` WHERE updated_at <= :high ORDER BY `{pk}`"), {"high": high})
    count, last = 0, None
    while rows := result.fetchmany(batch_size):
        sim.execute(_INSERT_EVENT, [_event(schema.name, pk, int(r[pk_index]), "SNAPSHOT", None, image(schema, r),
                                           r[ts_index], batch, run, now) for r in rows])
        count += len(rows)
        for r in rows:
            key = (r[ts_index], int(r[pk_index]))
            last = key if last is None or key > last else last
    result.close()
    _set_mark(sim, schema.name, last or (None, None), high, count, now)
    return count


def _changes(source: Connection, sim: Connection, schema: TableSchema, mark: tuple, high: datetime, batch: str,
             run: int | None, now: datetime) -> tuple[list[dict[str, Any]], tuple | None]:
    pk = schema.primary_key
    ts_index, pk_index = schema.names.index("updated_at"), schema.names.index(pk)
    after = "" if mark[1] is None else "AND (updated_at > :ts OR (updated_at = :ts AND `{0}` > :pk))".format(pk)
    rows = source.execute(text(
        f"SELECT {_select(schema)} FROM `{schema.name}` WHERE updated_at <= :high {after} ORDER BY updated_at, `{pk}`"),
        {"high": high, "ts": mark[1], "pk": mark[2]}).fetchall()
    if not rows:
        return [], None
    previous = last_images(sim, schema.name, [int(r[pk_index]) for r in rows])
    events = []
    for row in rows:
        key = int(row[pk_index])
        current = image(schema, row)
        before = previous.get(key)
        if before is None:
            events.append(_event(schema.name, pk, key, "INSERT", None, current, row[ts_index], batch, run, now))
        elif before != current:
            events.append(_event(schema.name, pk, key, "UPDATE", before, current, row[ts_index], batch, run, now))
    last = (rows[-1][ts_index], int(rows[-1][pk_index]))
    return events, last


# ------------------------------------------------------------------------------------------ consumer

_META = [
    "CREATE SCHEMA IF NOT EXISTS cdc_current",
    "CREATE SCHEMA IF NOT EXISTS cdc_history",
    "CREATE SCHEMA IF NOT EXISTS etl_meta",
    """CREATE TABLE IF NOT EXISTS etl_meta.cdc_offsets (
           consumer VARCHAR PRIMARY KEY, last_lsn UBIGINT NOT NULL, events_applied UBIGINT NOT NULL,
           events_rejected UBIGINT NOT NULL, updated_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS etl_meta.cdc_rejected_events (
           change_event_id UBIGINT PRIMARY KEY, source_table VARCHAR NOT NULL, row_pk UBIGINT NOT NULL,
           operation VARCHAR NOT NULL, reason VARCHAR NOT NULL, event_updated_at TIMESTAMP,
           held_updated_at TIMESTAMP, is_simulated_anomaly BOOLEAN, logged_at TIMESTAMP NOT NULL)""",
]
CONSUMER = "duckdb_cdc"


@dataclass
class ApplyResult:
    path: Path
    from_lsn: int
    to_lsn: int
    applied: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    tables: set[str] = field(default_factory=set)
    seconds: float = 0.0


def _q(name: str) -> str:
    return f'"{name}"'


def _ensure_tables(con: duckdb.DuckDBPyConnection, schema: TableSchema) -> None:
    columns = ", ".join(f"{_q(c.name)} {c.duckdb_type}" for c in schema.columns)
    con.execute(f"CREATE TABLE IF NOT EXISTS cdc_current.{_q(schema.name)} ({columns}, "
                f"PRIMARY KEY ({_q(schema.primary_key)}))")
    con.execute(f"CREATE TABLE IF NOT EXISTS cdc_history.{_q(schema.name)} ({columns}, _lsn UBIGINT NOT NULL, "
                f"_operation VARCHAR NOT NULL, _valid_from TIMESTAMP NOT NULL, _valid_to TIMESTAMP, "
                f"_is_current BOOLEAN NOT NULL)")


def apply(settings: Settings, code: str, batch_size: int = 20000) -> ApplyResult:
    """Apply the change events after the consumer's offset to ``<code>_sim.duckdb`` (idempotent)."""
    clock = time.perf_counter()
    workspace = get_workspace(settings, code)
    path = Path(workspace.duckdb_path).with_name(f"{workspace.code}_sim.duckdb")
    path.parent.mkdir(parents=True, exist_ok=True)
    source_engine, sim_engine = _engines(settings, workspace)
    con = duckdb.connect(str(path))
    try:
        for statement in _META:
            con.execute(statement)
        offset = con.execute("SELECT last_lsn FROM etl_meta.cdc_offsets WHERE consumer = ?", [CONSUMER]).fetchone()
        last_lsn = int(offset[0]) if offset else 0
        result = ApplyResult(path, last_lsn, last_lsn)
        with source_engine.connect() as source, sim_engine.connect() as sim:
            ensure_control(sim)
            schemas = discover(source)
            while True:
                events = sim.execute(text("""
                    SELECT change_event_id, source_table, operation, row_pk, after_image, before_image,
                           source_committed_at, is_simulated_anomaly
                    FROM change_event_log WHERE change_event_id > :lsn ORDER BY change_event_id LIMIT :n"""),
                    {"lsn": last_lsn, "n": batch_size}).fetchall()
                if not events:
                    break
                _apply_batch(con, schemas, events, result)
                last_lsn = int(events[-1][0])
                result.to_lsn = last_lsn
        result.seconds = round(time.perf_counter() - clock, 2)
        return result
    finally:
        con.close()
        source_engine.dispose()
        sim_engine.dispose()


def _load_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return value if isinstance(value, dict) else json.loads(value)


def _apply_batch(con, schemas: dict[str, TableSchema], events: list[tuple], result: ApplyResult) -> None:
    by_table: dict[str, list[tuple]] = {}
    for event in events:
        by_table.setdefault(event[1], []).append(event)
    now = wall_clock()
    con.execute("BEGIN TRANSACTION")
    try:
        applied = rejected = 0
        for table, table_events in by_table.items():
            schema = schemas.get(table)
            if schema is None:
                continue
            _ensure_tables(con, schema)
            pk = schema.primary_key
            keys = sorted({int(e[3]) for e in table_events})
            held: dict[int, datetime | None] = {}
            for start in range(0, len(keys), 5000):
                chunk = keys[start:start + 5000]
                for key, stamp in con.execute(
                        f"SELECT {_q(pk)}, updated_at FROM cdc_current.{_q(table)} WHERE {_q(pk)} IN "
                        f"({', '.join(map(str, chunk))})").fetchall():
                    held[int(key)] = stamp
            upserts: dict[int, tuple] = {}
            deletes: set[int] = set()
            history: list[tuple] = []
            for lsn, _, op, key, after, before, committed_at, is_anomaly in table_events:
                key = int(key)
                if op == "DELETE":
                    data = _load_json(before)
                    upserts.pop(key, None)
                    deletes.add(key)
                    held[key] = None
                    history.append(image_row(schema, data) + (int(lsn), op, committed_at, None, False))
                    applied += 1
                    continue
                data = _load_json(after)
                stamp = datetime.fromisoformat(data["updated_at"])
                current = held.get(key)
                if current is not None and stamp <= current:
                    reason = "DUPLICATE" if stamp == current else "STALE"
                    con.execute("INSERT OR IGNORE INTO etl_meta.cdc_rejected_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                [int(lsn), table, key, op, reason, stamp, current, bool(is_anomaly), now])
                    result.rejected[reason] = result.rejected.get(reason, 0) + 1
                    rejected += 1
                    continue
                row = image_row(schema, data)
                upserts[key] = row
                deletes.discard(key)
                held[key] = stamp
                history.append(row + (int(lsn), op, stamp, None, False))
                applied += 1
            if deletes:
                con.execute(f"DELETE FROM cdc_current.{_q(table)} WHERE {_q(pk)} IN ({', '.join(map(str, deletes))})")
            if upserts:
                batch = to_arrow(schema, list(upserts.values()))
                con.register("cdc_batch", batch)
                con.execute(f"INSERT OR REPLACE INTO cdc_current.{_q(table)} SELECT * FROM cdc_batch")
                con.unregister("cdc_batch")
            if history:
                _append_history(con, schema, history)
            result.tables.add(table)
        last = int(events[-1][0])
        con.execute("""INSERT INTO etl_meta.cdc_offsets VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (consumer) DO UPDATE SET last_lsn = excluded.last_lsn,
                           events_applied = cdc_offsets.events_applied + excluded.events_applied,
                           events_rejected = cdc_offsets.events_rejected + excluded.events_rejected,
                           updated_at = excluded.updated_at""", [CONSUMER, last, applied, rejected, now])
        con.execute("COMMIT")
        result.applied += applied
    except Exception:
        con.execute("ROLLBACK")
        raise


def _append_history(con, schema: TableSchema, rows: list[tuple]) -> None:
    import pyarrow as pa

    base = to_arrow(schema, [r[:len(schema.columns)] for r in rows])
    extra = [pa.array([r[-5] for r in rows], pa.uint64()), pa.array([r[-4] for r in rows], pa.string()),
             pa.array([r[-3] for r in rows], pa.timestamp("us")), pa.array([None] * len(rows), pa.timestamp("us")),
             pa.array([False] * len(rows), pa.bool_())]
    table = pa.Table.from_arrays(list(base.columns) + extra,
                                 names=schema.names + ["_lsn", "_operation", "_valid_from", "_valid_to", "_is_current"])
    name, pk = _q(schema.name), _q(schema.primary_key)
    con.register("cdc_history_batch", table)
    con.execute(f"INSERT INTO cdc_history.{name} SELECT * FROM cdc_history_batch")
    con.execute(f"""
        UPDATE cdc_history.{name} AS h SET _valid_to = n.next_from,
               _is_current = (n.next_from IS NULL AND h._operation <> 'DELETE')
        FROM (SELECT _lsn, LEAD(_valid_from) OVER (PARTITION BY {pk} ORDER BY _lsn) AS next_from
              FROM cdc_history.{name}
              WHERE {pk} IN (SELECT {pk} FROM cdc_history_batch)) AS n
        WHERE h._lsn = n._lsn""")
    con.unregister("cdc_history_batch")


# ------------------------------------------------------------------------------------------ checks


def compare(settings: Settings, code: str) -> dict[str, str]:
    """CDC target vs source per captured table: PASSED / FAILED (with reason) / SKIPPED (source moved on)."""
    workspace = get_workspace(settings, code)
    path = Path(workspace.duckdb_path).with_name(f"{workspace.code}_sim.duckdb")
    if not path.exists():
        raise CdcError(f"No CDC target yet: run `scmgen simulate cdc apply -w {code}`")
    source_engine, sim_engine = _engines(settings, workspace)
    con = duckdb.connect(str(path), read_only=True)
    outcome: dict[str, str] = {}
    try:
        with source_engine.connect() as source, sim_engine.connect() as sim:
            committed = source.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
            schemas = discover(source)
            marks = dict(sim.execute(text("SELECT source_table, high_water_mark FROM cdc_capture_watermarks")).fetchall())
            existing = {t for (t,) in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'cdc_current'").fetchall()}
            for table, high in sorted(marks.items()):
                if high != committed:
                    outcome[table] = "SKIPPED: source committed past the last capture"
                    continue
                schema = schemas[table]
                decimals = [c.name for c in schema.columns if c.duckdb_type.startswith("DECIMAL")]
                src = source.execute(text(
                    f"SELECT COUNT(*), MAX(updated_at){''.join(f', SUM(`{c}`)' for c in decimals)} FROM `{table}`")).fetchone()
                if table in existing:
                    dst = con.execute(
                        f"SELECT COUNT(*), MAX(updated_at){''.join(f', SUM({_q(c)})' for c in decimals)} "
                        f"FROM cdc_current.{_q(table)}").fetchone()
                else:                                         # no event yet: an empty target
                    dst = (0, None, *([None] * len(decimals)))
                problems = [] if src[0] == dst[0] else [f"rows {dst[0]} vs source {src[0]}"]
                if src[1] != dst[1]:
                    problems.append(f"max updated_at {dst[1]} vs {src[1]}")
                problems += [f"Σ {c}" for c, a, b in zip(decimals, src[2:], dst[2:]) if Decimal(a or 0) != Decimal(b or 0)]
                outcome[table] = "PASSED" if not problems else "FAILED: " + "; ".join(problems)
    finally:
        con.close()
        source_engine.dispose()
        sim_engine.dispose()
    return outcome


def rejected_rows(settings: Settings, code: str) -> list[tuple[int, str, str, bool]]:
    """Every rejected event: (LSN, table, reason, is_simulated_anomaly)."""
    workspace = get_workspace(settings, code)
    path = Path(workspace.duckdb_path).with_name(f"{workspace.code}_sim.duckdb")
    if not path.exists():
        return []
    con = duckdb.connect(str(path), read_only=True)
    try:
        return [(int(a), b, c, bool(d)) for a, b, c, d in con.execute(
            "SELECT change_event_id, source_table, reason, is_simulated_anomaly FROM etl_meta.cdc_rejected_events"
        ).fetchall()]
    except duckdb.CatalogException:
        return []
    finally:
        con.close()
