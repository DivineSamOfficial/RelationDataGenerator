"""Anomaly injectors (1G §9.2-§9.4; master prompt §14, §15D-E; D73, D91).

Every injector works on ``scm_<code>_sim`` only, through the ``scm_anomaly`` user (which can't write ``_oltp``),
picks its targets with a seeded RNG (same scenario + seed ⇒ same anomalies) and returns one registry record per
damaged row with the checks that **must** fail because of it (the expected-failure contract, §15E):

========================  ==========================================================  ==============================
type                      what it does                                                must trip
========================  ==========================================================  ==============================
duplicate_rows            copies rows with a new PK and the same business key          DQ-UNIQUE on the table's keys
null_mandatory            NULL into a NOT NULL column (the copy's column relaxed)      DQ-NOT_NULL:<table>.<column>
orphan_reference          a foreign-key value with no parent                           DQ-FK:<table>.<column>
timestamp_reversed        ``updated_at`` set before ``created_at``                     DQ-TIMESTAMP, V-SY-03
future_business_date      a business date moved past the simulated present             ``expect`` in the config
invoice_line_amount       an invoice line's taxable amount changed                     ``expect`` in the config
journal_imbalance         one journal line's amount changed                            ``expect`` in the config
inventory_drift           an inventory balance no longer matches its ledger            ``expect`` in the config
status_out_of_order       two tracking events of a shipment swap times                 V-SH-03
late_arriving             a copied row inserted with timestamps before the watermark   LD-RECON (+ DQ-UNIQUE)
hard_delete               rows physically deleted                                      LD-RECON:<table> (+ DQ-FK)
schema_change             a column added to the copy                                   DQ-SCHEMA, LD-SCHEMA_DRIFT
duplicate_cdc_events      genuine change events re-delivered with new LSNs             CDC-DUPLICATE:<table>
stale_cdc_events          an older row version delivered after a newer one             CDC-STALE:<table>
========================  ==========================================================  ==============================
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import wall_clock
from scmgen.core.errors import ScmgenError
from scmgen.core.settings import Settings
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import get_workspace
from scmgen.simulation import sim_schema
from scmgen.simulation.config import Scenario, load_simulation_config


class AnomalyError(ScmgenError):
    """An injection can't run (unknown scenario or type, no staging copies, prerequisite missing)."""


# Built-in expectations of an injector type, on top of the ``expect`` / ``may_fail`` lists of each injection in
# config/simulation.yaml (business-rule checks depend on which table and column are damaged, so they are data).
BUILT_IN: dict[str, list[str]] = {"timestamp_reversed": ["V-SY-03"], "status_out_of_order": ["V-SH-03"]}
# Injectors that change rows without moving ``updated_at`` forward (a "silent update"): an incremental load can't
# see them, so when the table is already in the sim replica its reconciliation may fail (counts and DECIMAL sums).
SILENT = frozenset({"duplicate_rows", "null_mandatory", "orphan_reference", "timestamp_reversed",
                    "future_business_date", "invoice_line_amount", "journal_imbalance", "inventory_drift",
                    "status_out_of_order"})


@dataclass
class Record:
    anomaly_type: str
    table: str
    pk: int | None
    column: str | None
    original: Any
    injected: Any
    expected: list[str] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)     # may fail too, depending on the rows hit


@dataclass
class Context:
    conn: Connection
    oltp: str
    sim: str
    rng: random.Random
    committed_through: datetime | None
    settings: Settings
    code: str

    def pk(self, table: str) -> str:
        return self.conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
            "AND CONSTRAINT_NAME = 'PRIMARY'"), {"s": self.sim, "t": table}).scalar()

    def pick(self, table: str, count: int, where: str = "1 = 1", column: str | None = None) -> list[int]:
        pk = column or self.pk(table)
        ids = [int(r[0]) for r in self.conn.execute(text(
            f"SELECT `{pk}` FROM `{table}` WHERE {where} ORDER BY `{pk}` LIMIT 20000"))]
        if not ids:
            raise AnomalyError(f"{table}: no row matches the injection's target condition")
        return sorted(self.rng.sample(ids, min(count, len(ids))))

    def next_id(self, table: str) -> int:
        pk = self.pk(table)
        return int(self.conn.execute(text(f"SELECT COALESCE(MAX(`{pk}`), 0) FROM `{table}`")).scalar()) + 1

    def insert_columns(self, table: str) -> list[str]:
        return [c for (c,) in self.conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
            "AND COALESCE(GENERATION_EXPRESSION, '') = '' ORDER BY ORDINAL_POSITION"), {"s": self.sim, "t": table})]

    def relax(self, table: str, column: str) -> None:
        """Make a NOT NULL column of the copy nullable (the source lost the constraint)."""
        column_type = self.conn.execute(text(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
            "AND COLUMN_NAME = :c"), {"s": self.sim, "t": table, "c": column}).scalar()
        if column_type is None:
            raise AnomalyError(f"{table}.{column} doesn't exist")
        execute_raw(self.conn, f"ALTER TABLE `{table}` MODIFY `{column}` {column_type} NULL")

    def unique_keys(self, table: str) -> list[str]:
        return sorted({i for (i,) in self.conn.execute(text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
            "AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY'"), {"s": self.oltp, "t": table})})

    def value(self, table: str, pk: int, column: str) -> Any:
        return self.conn.execute(text(f"SELECT `{column}` FROM `{table}` WHERE `{self.pk(table)}` = :id"),
                                 {"id": pk}).scalar()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


# ------------------------------------------------------------------------------------------ injectors


def duplicate_rows(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    pk = ctx.pk(table)
    columns = [c for c in ctx.insert_columns(table) if c != pk]
    records = []
    for source in ctx.pick(table, spec.get("count", 1)):
        new = ctx.next_id(table)
        listed = ", ".join(f"`{c}`" for c in columns)
        ctx.conn.execute(text(f"INSERT INTO `{table}` (`{pk}`, {listed}) SELECT :new, {listed} FROM `{table}` "
                              f"WHERE `{pk}` = :src"), {"new": new, "src": source})
        records.append(Record("duplicate_rows", table, new, None, source, new, _copied_keys(ctx, table, source)))
    return records


def _copied_keys(ctx: Context, table: str, source: int) -> list[str]:
    """The UNIQUE business keys a copy of row ``source`` duplicates (those with no NULL column)."""
    keys = []
    for index in ctx.unique_keys(table):
        cols = [c for (c,) in ctx.conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
            "AND INDEX_NAME = :i"), {"s": ctx.oltp, "t": table, "i": index})]
        if ctx.pk(table) not in cols and all(ctx.value(table, source, c) is not None for c in cols):
            keys.append(f"DQ-UNIQUE:{table}.{index}")
    return keys


def null_mandatory(ctx: Context, spec: dict) -> list[Record]:
    table, column = spec["table"], spec["column"]
    ctx.relax(table, column)
    pk = ctx.pk(table)
    records = []
    for target in ctx.pick(table, spec.get("count", 1), f"`{column}` IS NOT NULL"):
        original = ctx.value(table, target, column)
        ctx.conn.execute(text(f"UPDATE `{table}` SET `{column}` = NULL WHERE `{pk}` = :id"), {"id": target})
        records.append(Record("null_mandatory", table, target, column, _str(original), None,
                              [f"DQ-NOT_NULL:{table}.{column}"]))
    return records


def orphan_reference(ctx: Context, spec: dict) -> list[Record]:
    table, column = spec["table"], spec["column"]
    parent, parent_column = ctx.conn.execute(text(
        "SELECT REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t AND COLUMN_NAME = :c AND REFERENCED_TABLE_NAME IS NOT NULL "
        "AND ORDINAL_POSITION = 1 LIMIT 1"), {"s": ctx.oltp, "t": table, "c": column}).fetchone() or (None, None)
    if parent is None:
        raise AnomalyError(f"{table}.{column} is not a foreign key")
    missing = int(ctx.conn.execute(text(f"SELECT COALESCE(MAX(`{parent_column}`), 0) FROM `{parent}`")).scalar()) + 1000
    pk = ctx.pk(table)
    records = []
    for offset, target in enumerate(ctx.pick(table, spec.get("count", 1), f"`{column}` IS NOT NULL")):
        original = ctx.value(table, target, column)
        ctx.conn.execute(text(f"UPDATE `{table}` SET `{column}` = :v WHERE `{pk}` = :id"),
                         {"v": missing + offset, "id": target})
        records.append(Record("orphan_reference", table, target, column, _str(original), str(missing + offset),
                              [f"DQ-FK:{table}.{column}"]))
    return records


def timestamp_reversed(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    pk = ctx.pk(table)
    records = []
    for target in ctx.pick(table, spec.get("count", 1)):
        original = ctx.value(table, target, "updated_at")
        ctx.conn.execute(text(f"UPDATE `{table}` SET updated_at = created_at - INTERVAL 1 HOUR WHERE `{pk}` = :id"),
                         {"id": target})
        records.append(Record("timestamp_reversed", table, target, "updated_at", _str(original),
                              _str(ctx.value(table, target, "updated_at")),
                              [f"DQ-TIMESTAMP:{table}"]))
    return records


def future_business_date(ctx: Context, spec: dict) -> list[Record]:
    table, column = spec["table"], spec["column"]
    pk = ctx.pk(table)
    future = (ctx.committed_through or wall_clock()).date() + timedelta(days=30)
    records = []
    for target in ctx.pick(table, spec.get("count", 1)):
        original = ctx.value(table, target, column)
        ctx.conn.execute(text(f"UPDATE `{table}` SET `{column}` = :d WHERE `{pk}` = :id"), {"d": future, "id": target})
        records.append(Record("future_business_date", table, target, column, _str(original), str(future)))
    return records


def _amount_change(kind: str, column: str, delta: str, where: str = "1 = 1") -> Callable[[Context, dict], list[Record]]:
    def inject(ctx: Context, spec: dict) -> list[Record]:
        table = spec["table"]
        pk = ctx.pk(table)
        records = []
        for target in ctx.pick(table, spec.get("count", 1), where):
            original = ctx.value(table, target, column)
            ctx.conn.execute(text(f"UPDATE `{table}` SET `{column}` = `{column}` + {delta} WHERE `{pk}` = :id"),
                             {"id": target})
            records.append(Record(kind, table, target, column, _str(original), _str(ctx.value(table, target, column))))
        return records
    return inject


def status_out_of_order(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    if table != "shipment_tracking":
        raise AnomalyError("status_out_of_order is defined for shipment_tracking")
    shipments = ctx.pick(table, spec.get("count", 1), "event_sequence = 2", column="shipment_id")
    records = []
    for shipment in shipments:
        first, second = ctx.conn.execute(text(
            "SELECT shipment_tracking_id, event_at FROM shipment_tracking WHERE shipment_id = :s AND event_sequence IN (1, 2) "
            "ORDER BY event_sequence"), {"s": shipment}).fetchall()
        ctx.conn.execute(text("UPDATE shipment_tracking SET event_at = :t WHERE shipment_tracking_id = :id"),
                         {"t": second[1], "id": first[0]})
        ctx.conn.execute(text("UPDATE shipment_tracking SET event_at = :t WHERE shipment_tracking_id = :id"),
                         {"t": first[1], "id": second[0]})
        records.append(Record("status_out_of_order", table, int(first[0]), "event_at", _str(first[1]), _str(second[1])))
    return records


def _replica_mark(ctx: Context, table: str) -> datetime | None:
    import duckdb

    from scmgen.loader.run import replica_path
    path = replica_path(get_workspace(ctx.settings, ctx.code), "sim")
    if not path.exists():
        return None
    con = duckdb.connect(str(path), read_only=True)
    try:
        row = con.execute("SELECT last_watermark_ts FROM etl_meta.etl_table_watermarks WHERE source_table = ?",
                          [table]).fetchone()
        return row[0] if row else None
    except duckdb.CatalogException:
        return None
    finally:
        con.close()


def late_arriving(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    mark = _replica_mark(ctx, table)
    if mark is None:
        raise AnomalyError(f"late_arriving needs a sim replica of {table}: run `scmgen load -w {ctx.code} --source sim`")
    pk = ctx.pk(table)
    columns = [c for c in ctx.insert_columns(table) if c not in (pk, "created_at", "updated_at")]
    stamp = mark - timedelta(days=1)
    records = []
    for source in ctx.pick(table, spec.get("count", 1)):
        new = ctx.next_id(table)
        listed = ", ".join(f"`{c}`" for c in columns)
        ctx.conn.execute(text(f"INSERT INTO `{table}` (`{pk}`, {listed}, created_at, updated_at) "
                              f"SELECT :new, {listed}, :ts, :ts FROM `{table}` WHERE `{pk}` = :src"),
                         {"new": new, "src": source, "ts": stamp})
        records.append(Record("late_arriving", table, new, "updated_at", None, str(stamp),
                              [f"LD-RECON:{table}", *_copied_keys(ctx, table, source)]))
    return records


def hard_delete(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    pk = ctx.pk(table)
    children = [(t, c) for t, c in ctx.conn.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = :s "
        "AND REFERENCED_TABLE_NAME = :t AND ORDINAL_POSITION = 1 AND TABLE_NAME <> :t"), {"s": ctx.oltp, "t": table})]
    records = []
    for target in ctx.pick(table, spec.get("count", 1)):
        row = ctx.conn.execute(text(f"SELECT * FROM `{table}` WHERE `{pk}` = :id"), {"id": target}).mappings().first()
        orphans = [f"DQ-FK:{t}.{c}" for t, c in children if ctx.conn.execute(text(
            f"SELECT COUNT(*) FROM `{t}` WHERE `{c}` = :id"), {"id": target}).scalar()]
        ctx.conn.execute(text(f"DELETE FROM `{table}` WHERE `{pk}` = :id"), {"id": target})
        records.append(Record("hard_delete", table, target, None, json.dumps({k: _str(v) for k, v in row.items()}),
                              None, [f"LD-RECON:{table}", *orphans]))
    return records


def schema_change(ctx: Context, spec: dict) -> list[Record]:
    table, column = spec["table"], spec.get("column", "legacy_ref")
    execute_raw(ctx.conn, f"ALTER TABLE `{table}` ADD COLUMN `{column}` VARCHAR(30) NULL")
    pk = ctx.pk(table)
    targets = ctx.pick(table, spec.get("count", 3))
    ctx.conn.execute(text(f"UPDATE `{table}` SET `{column}` = CONCAT('LEGACY-', `{pk}`) WHERE `{pk}` IN "
                          f"({', '.join(map(str, targets))})"))
    expected = [f"DQ-SCHEMA:{table}"]
    if _replica_mark(ctx, table) is not None or _replica_has(ctx, table):
        expected.append(f"LD-SCHEMA_DRIFT:{table}")
    return [Record("schema_change", table, None, column, None, "VARCHAR(30) NULL", expected)]


def _replica_has(ctx: Context, table: str) -> bool:
    import duckdb

    from scmgen.loader.run import replica_path
    path = replica_path(get_workspace(ctx.settings, ctx.code), "sim")
    if not path.exists():
        return False
    con = duckdb.connect(str(path), read_only=True)
    try:
        return bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'raw' "
                                "AND table_name = ?", [table]).fetchone()[0])
    finally:
        con.close()


def _cdc_events(ctx: Context, table: str, count: int, where: str = "1 = 1") -> list[tuple]:
    events = ctx.conn.execute(text(
        f"SELECT change_event_id FROM change_event_log WHERE source_table = :t AND NOT is_simulated_anomaly AND {where} "
        "ORDER BY change_event_id DESC LIMIT 5000"), {"t": table}).fetchall()
    if not events:
        raise AnomalyError(f"No change events for {table}: run `scmgen simulate cdc capture -w {ctx.code}` first")
    chosen = sorted(ctx.rng.sample([int(e[0]) for e in events], min(count, len(events))))
    return [ctx.conn.execute(text("SELECT * FROM change_event_log WHERE change_event_id = :id"), {"id": e}).mappings().one()
            for e in chosen]


_COPY_EVENT = text("""
    INSERT INTO change_event_log (source_table, operation, row_pk, primary_key, before_image, after_image,
        changed_columns, source_committed_at, capture_batch_id, generation_run_id, is_simulated_anomaly, anomaly_id,
        captured_at, created_at, updated_at)
    VALUES (:source_table, :operation, :row_pk, :primary_key, :before_image, :after_image, :changed_columns,
            :source_committed_at, :capture_batch_id, :generation_run_id, TRUE, NULL, :now, :now, :now)""")


def _json(value: Any) -> str | None:
    return None if value is None else (value if isinstance(value, str) else json.dumps(value))


def duplicate_cdc_events(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    records = []
    for event in _cdc_events(ctx, table, spec.get("count", 1), "operation <> 'DELETE'"):
        row = dict(event)
        for key in ("primary_key", "before_image", "after_image", "changed_columns"):
            row[key] = _json(row[key])
        ctx.conn.execute(_COPY_EVENT, {**row, "now": wall_clock()})
        new = int(ctx.conn.execute(text("SELECT LAST_INSERT_ID()")).scalar())
        records.append(Record("duplicate_cdc_events", "change_event_log", new, None, str(event["change_event_id"]),
                              str(new), [f"CDC-DUPLICATE:{table}"]))
    return records


def stale_cdc_events(ctx: Context, spec: dict) -> list[Record]:
    table = spec["table"]
    records = []
    for event in _cdc_events(ctx, table, spec.get("count", 1), "operation <> 'DELETE'"):
        after = event["after_image"] if isinstance(event["after_image"], dict) else json.loads(event["after_image"])
        before = event["before_image"]
        if before is not None:                 # the version the UPDATE replaced, delivered again after it
            stale = before if isinstance(before, dict) else json.loads(before)
        else:                                  # no earlier version captured: a fabricated older one
            stale = dict(after)
            stale["updated_at"] = (datetime.fromisoformat(after["updated_at"]) - timedelta(days=1)).isoformat(
                timespec="microseconds")
        row = dict(event)
        row.update(operation="UPDATE", before_image=json.dumps(after), after_image=json.dumps(stale),
                   primary_key=_json(row["primary_key"]), changed_columns=json.dumps(
                       sorted(k for k in stale if stale.get(k) != after.get(k))),
                   source_committed_at=datetime.fromisoformat(stale["updated_at"]))
        ctx.conn.execute(_COPY_EVENT, {**row, "now": wall_clock()})
        new = int(ctx.conn.execute(text("SELECT LAST_INSERT_ID()")).scalar())
        records.append(Record("stale_cdc_events", "change_event_log", new, None, after.get("updated_at"),
                              stale["updated_at"], [f"CDC-STALE:{table}"]))
    return records


INJECTORS: dict[str, Callable[[Context, dict], list[Record]]] = {
    "duplicate_rows": duplicate_rows,
    "null_mandatory": null_mandatory,
    "orphan_reference": orphan_reference,
    "timestamp_reversed": timestamp_reversed,
    "future_business_date": future_business_date,
    "invoice_line_amount": _amount_change("invoice_line_amount", "taxable_amount", "100"),
    "journal_imbalance": _amount_change("journal_imbalance", "debit_amount", "1", "debit_amount > 0"),
    "inventory_drift": _amount_change("inventory_drift", "qty_sellable", "5"),
    "status_out_of_order": status_out_of_order,
    "late_arriving": late_arriving,
    "hard_delete": hard_delete,
    "schema_change": schema_change,
    "duplicate_cdc_events": duplicate_cdc_events,
    "stale_cdc_events": stale_cdc_events,
}


# ------------------------------------------------------------------------------------------ run


@dataclass
class InjectionResult:
    scenario: str
    anomaly_scenario_id: int
    seed: int
    records: list[Record]


def inject(settings: Settings, code: str, scenario_name: str, seed: int = 1) -> InjectionResult:
    """Run one scenario of ``config/simulation.yaml`` on the staging copies and register every anomaly."""
    config = load_simulation_config()
    scenario: Scenario | None = config.scenarios.get(scenario_name)
    if scenario is None:
        raise AnomalyError(f"Unknown anomaly scenario {scenario_name!r}: one of {sorted(config.scenarios)}")
    unknown = sorted({i["type"] for i in scenario.injections} - set(INJECTORS))
    if unknown:
        raise AnomalyError(f"Scenario {scenario_name}: unknown injection type(s) {unknown}")
    workspace = get_workspace(settings, code)
    engine = sim_schema.sim_engine(settings, workspace)
    try:
        with engine.connect() as conn:
            if not sim_schema.info(conn).get("sim_build_id"):
                raise AnomalyError(f"No staging copies yet: run `scmgen simulate reset -w {code}` first")
            committed = conn.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
            started = wall_clock()
            conn.execute(text("""INSERT INTO anomaly_scenarios (scenario_name, scenario_config, seed, scenario_status,
                                     started_at, created_at, updated_at)
                                 VALUES (:n, :c, :s, 'RUNNING', :t, :t, :t)"""),
                         {"n": scenario_name, "c": json.dumps({"description": scenario.description,
                                                               "injections": scenario.injections}),
                          "s": seed, "t": started})
            scenario_id = int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar())
            conn.commit()
            ctx = Context(conn, workspace.oltp_schema, workspace.sim_schema, random.Random(f"{seed}:{scenario_name}"),
                          committed, settings, code)
            records: list[Record] = []
            try:
                for spec in scenario.injections:
                    silent = spec["type"] in SILENT and _replica_has(ctx, spec["table"])
                    for record in INJECTORS[spec["type"]](ctx, spec):
                        record.expected = _unique(record.expected + BUILT_IN.get(spec["type"], [])
                                                  + list(spec.get("expect") or []))
                        record.collateral = _unique(list(spec.get("may_fail") or [])
                                                    + ([f"LD-RECON:{spec['table']}"] if silent else []))
                        records.append(record)
                now = wall_clock()
                for record in records:
                    conn.execute(text("""
                        INSERT INTO anomaly_registry (anomaly_scenario_id, anomaly_type, target_table, target_pk,
                            target_column, original_value, injected_value, expected_failing_checks, collateral_checks,
                            injected_at, created_at, updated_at)
                        VALUES (:s, :type, :t, :pk, :c, :o, :i, :e, :m, :now, :now, :now)"""),
                        {"s": scenario_id, "type": record.anomaly_type, "t": record.table, "pk": record.pk,
                         "c": record.column, "o": _str(record.original), "i": _str(record.injected),
                         "e": json.dumps(record.expected), "m": json.dumps(record.collateral), "now": now})
                    if record.table == "change_event_log":
                        conn.execute(text("UPDATE change_event_log SET anomaly_id = LAST_INSERT_ID() "
                                          "WHERE change_event_id = :id"), {"id": record.pk})
                conn.execute(text("""UPDATE anomaly_scenarios SET scenario_status = 'COMPLETED', anomalies_injected = :n,
                                         finished_at = :t, updated_at = :t WHERE anomaly_scenario_id = :id"""),
                             {"n": len(records), "t": wall_clock(), "id": scenario_id})
                conn.commit()
            except Exception as exc:
                conn.rollback()
                conn.execute(text("""UPDATE anomaly_scenarios SET scenario_status = 'FAILED', error_message = :m,
                                         finished_at = :t, updated_at = :t WHERE anomaly_scenario_id = :id"""),
                             {"m": str(exc)[:2000], "t": wall_clock(), "id": scenario_id})
                conn.commit()
                raise
    finally:
        engine.dispose()
    return InjectionResult(scenario_name, scenario_id, seed, records)
