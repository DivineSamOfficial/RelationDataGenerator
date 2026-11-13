"""The simulation schema ``scm_<code>_sim`` (1G §9.1-§9.3, D73, D90).

* Control tables (``change_event_log``, ``cdc_capture_watermarks``, ``sim_copy_info``, ``anomaly_scenarios``,
  ``anomaly_registry``) come from ``db/ddl/sim``.
* **Staging copies** of the 92 workspace tables are made by ``reset`` as the ``scm_anomaly`` user, which can read
  ``_oltp`` but not write it: ``CREATE TABLE … LIKE`` (columns, PK, indexes), then the CHECK constraints are dropped
  and UNIQUE indexes become plain indexes (``LIKE`` copies no foreign keys), so injected anomalies — duplicates,
  orphans, wrong totals — can be stored, exactly like a real source system that lost its constraints. The indexes
  stay, so the validation suite runs at the same speed as on ``_oltp``.
* ``reset`` re-copies everything, deletes the anomaly registry and the anomalous change events (genuine CDC events
  stay) and gives the copy a new ``sim_build_id``: the next sim load is then a full reload (like a rebuild, A2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import wall_clock
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw, read_sql, run_sql
from scmgen.db.workspaces import WorkspaceInfo, get_workspace

SIM_DDL = "ddl/sim/0001_sim_control.sql"
CONTROL_TABLES = ("change_event_log", "cdc_capture_watermarks", "sim_copy_info", "anomaly_scenarios",
                  "anomaly_registry")


@dataclass(frozen=True)
class ResetResult:
    tables: int
    rows: int
    sim_build_id: str
    seconds: float


def ensure_control(conn: Connection) -> None:
    """Create the sim control tables if missing (idempotent)."""
    run_sql(conn, read_sql(SIM_DDL))


def oltp_tables(conn: Connection, oltp_schema: str) -> list[str]:
    return [t for (t,) in conn.execute(text(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :s AND TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_NAME"), {"s": oltp_schema})]


def copied_tables(conn: Connection, sim_schema: str) -> list[str]:
    return [t for (t,) in conn.execute(text(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :s AND TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_NAME"), {"s": sim_schema}) if t not in CONTROL_TABLES]


def info(conn: Connection) -> dict[str, str]:
    try:
        return dict(conn.execute(text("SELECT info_key, info_value FROM sim_copy_info")).fetchall())
    except Exception:                                     # control tables not created yet
        return {}


def _set_info(conn: Connection, key: str, value: str | None) -> None:
    now = wall_clock()
    conn.execute(text("""INSERT INTO sim_copy_info (info_key, info_value, created_at, updated_at) VALUES (:k, :v, :t, :t)
                         ON DUPLICATE KEY UPDATE info_value = VALUES(info_value), updated_at = VALUES(updated_at)"""),
                 {"k": key, "v": value, "t": now})


def sim_engine(settings: Settings, workspace: WorkspaceInfo):
    """The anomaly tooling's connection (D73): SELECT on ``_oltp``, ALL on ``_sim``."""
    return create_mysql_engine(settings, role="anomaly", database=workspace.sim_schema)


def _copy_table(conn: Connection, oltp: str, sim: str, table: str) -> int:
    execute_raw(conn, f"DROP TABLE IF EXISTS `{sim}`.`{table}`")
    execute_raw(conn, f"CREATE TABLE `{sim}`.`{table}` LIKE `{oltp}`.`{table}`")
    checks = [c for (c,) in conn.execute(text(
        "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
        "AND CONSTRAINT_TYPE = 'CHECK'"), {"s": sim, "t": table})]
    uniques: dict[str, list[str]] = {}
    for name, column in conn.execute(text(
            "SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = :s "
            "AND TABLE_NAME = :t AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY' ORDER BY INDEX_NAME, SEQ_IN_INDEX"),
            {"s": sim, "t": table}):
        uniques.setdefault(name, []).append(column)
    changes = [f"DROP CHECK `{c}`" for c in checks]
    for name, columns in uniques.items():
        changes += [f"DROP INDEX `{name}`", f"ADD INDEX `{name}` ({', '.join(f'`{c}`' for c in columns)})"]
    if changes:
        execute_raw(conn, f"ALTER TABLE `{sim}`.`{table}` " + ", ".join(changes))
    columns = [c for (c,) in conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
        "AND COALESCE(GENERATION_EXPRESSION, '') = '' ORDER BY ORDINAL_POSITION"), {"s": oltp, "t": table})]
    listed = ", ".join(f"`{c}`" for c in columns)
    result = conn.execute(text(f"INSERT INTO `{sim}`.`{table}` ({listed}) SELECT {listed} FROM `{oltp}`.`{table}`"))
    return result.rowcount


def reset(settings: Settings, code: str) -> ResetResult:
    """Re-copy every ``_oltp`` table into ``_sim`` and discard all anomalies (1G §9.1 point 4)."""
    import time

    clock = time.perf_counter()
    workspace = get_workspace(settings, code)
    engine = sim_engine(settings, workspace)
    try:
        with engine.connect() as conn:
            # The anomaly tables are emptied by a reset anyway: recreating them keeps their shape current.
            execute_raw(conn, "DROP TABLE IF EXISTS anomaly_registry, anomaly_scenarios")
            ensure_control(conn)
            execute_raw(conn, "SET SESSION foreign_key_checks = 0")
            tables = oltp_tables(conn, workspace.oltp_schema)
            for stale in set(copied_tables(conn, workspace.sim_schema)) - set(tables):
                execute_raw(conn, f"DROP TABLE `{workspace.sim_schema}`.`{stale}`")   # e.g. a table an anomaly added
            rows = 0
            for table in tables:
                rows += _copy_table(conn, workspace.oltp_schema, workspace.sim_schema, table)
                conn.commit()
            conn.execute(text("DELETE FROM change_event_log WHERE is_simulated_anomaly"))
            committed = conn.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
            build = uuid.uuid4().hex[:12]
            for key, value in (("sim_build_id", build), ("source_build_id", workspace.build_id),
                               ("source_committed_through", committed.isoformat() if committed else None),
                               ("copied_at", wall_clock().isoformat()), ("schema_changes", "")):
                _set_info(conn, key, value)
            conn.commit()
    finally:
        engine.dispose()
    return ResetResult(len(tables), rows, build, round(time.perf_counter() - clock, 1))


def status(settings: Settings, code: str) -> dict:
    workspace = get_workspace(settings, code)
    engine = sim_engine(settings, workspace)
    try:
        with engine.connect() as conn:
            details = info(conn)
            copies = copied_tables(conn, workspace.sim_schema) if details else []
            anomalies = []
            if details:
                anomalies = [dict(r) for r in conn.execute(text("""
                    SELECT s.scenario_name, r.anomaly_type, r.target_table, COUNT(*) AS anomalies,
                           MAX(r.injected_at) AS injected_at
                    FROM anomaly_registry r JOIN anomaly_scenarios s USING (anomaly_scenario_id)
                    GROUP BY 1, 2, 3 ORDER BY 5, 1, 2""")).mappings()]
                events = conn.execute(text("""SELECT COUNT(*), COALESCE(SUM(is_simulated_anomaly), 0),
                                                     MAX(source_committed_at) FROM change_event_log""")).fetchone()
            else:
                events = (0, 0, None)
    finally:
        engine.dispose()
    return {"info": details, "copies": len(copies), "anomalies": anomalies,
            "events": {"total": events[0], "anomalous": int(events[1] or 0), "latest": events[2]}}


def copied_at(details: dict[str, str]) -> datetime | None:
    value = details.get("copied_at")
    return datetime.fromisoformat(value) if value else None
