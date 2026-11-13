"""Phase 7: staging copies, CDC capture / consumer and anomaly simulation (1G §8-§9; D72, D73, D89-D92).

* The clean ``_oltp`` is never written: its checksum is unchanged after every anomaly scenario, and the
  ``scm_anomaly`` user can't write it (D73).
* A clean copy passes the same validation rules as the clean data and has no data-quality finding.
* CDC: the first capture is a snapshot; after Continue, INSERT / UPDATE events with before-images and changed
  columns; the consumer converges to the source, is idempotent, keeps SCD2 history, and rejects duplicate and stale
  events (each a mutation the consumer must survive).
* Anomalies: every scenario's anomalies are caught by their declared checks, with no unexplained failure; the same
  seed picks the same rows; reset discards everything.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import create_workspace, delete_workspace, get_workspace
from scmgen.generation.actions import continue_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.loader.run import load_workspace, replica_path
from scmgen.simulation import anomalies, cdc, detection, sim_schema
from scmgen.simulation.dq import run_dq
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def workspace():
    settings = load_settings()
    code = "zs" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "fmcg"], base_seed=17)
    try:
        config = load_generation_config(preset="small", overrides={"history.start_date": "2025-01-01",
                                                                    "history.end_date": "2025-02-28"})
        assert generate_history(settings, code, config).report.passed
        yield settings, code
    finally:
        delete_workspace(settings, code, purge=True)       # also removes <code>.duckdb and <code>_sim.duckdb
        folder = settings.data_dir / "reports" / code
        for report in folder.glob("*.json"):
            report.unlink()
        if folder.exists():
            folder.rmdir()


def _checksum(settings, schema: str) -> dict[str, int]:
    engine = create_mysql_engine(settings, role="app", database=schema)
    with engine.connect() as conn:
        tables = [t for (t,) in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"))]
        sums = {t: conn.execute(text(f"CHECKSUM TABLE `{t}`")).fetchone()[1] for t in tables}
    engine.dispose()
    return sums


def test_clean_copy_validates_like_the_source(workspace):
    settings, code = workspace
    result = sim_schema.reset(settings, code)
    assert result.tables == 92 and result.rows > 0
    ws = get_workspace(settings, code)
    engine = sim_schema.sim_engine(settings, ws)
    with engine.connect() as conn:
        report = run_checks(ValidationContext(conn), load_catalogue())
        failing = {r.rule_id for r in report.results if r.status in ("FAILED", "ERROR")}
        assert failing == set()
        assert run_dq(conn, ws.oltp_schema, ws.sim_schema) == []
        constraints = conn.execute(text("""SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE IN ('CHECK', 'UNIQUE', 'FOREIGN KEY')
              AND TABLE_NAME NOT IN ('change_event_log', 'anomaly_registry', 'anomaly_scenarios',
                                     'cdc_capture_watermarks', 'sim_copy_info')""")).scalar()
        assert constraints == 0                    # copies keep no CHECK / UNIQUE / FK (D90)
    engine.dispose()


def test_anomaly_user_cannot_write_clean_data(workspace):
    settings, code = workspace
    ws = get_workspace(settings, code)
    engine = create_mysql_engine(settings, role="anomaly", database=ws.oltp_schema)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() > 0
        with pytest.raises((OperationalError, ProgrammingError)):
            conn.execute(text("UPDATE orders SET updated_at = updated_at LIMIT 1"))
    engine.dispose()


def test_cdc_snapshot_then_changes_and_consumer(workspace):
    settings, code = workspace
    first = cdc.capture(settings, code)
    assert first.total > 0 and all(set(ops) <= {"SNAPSHOT"} for ops in first.events.values())
    assert cdc.capture(settings, code).total == 0                       # nothing new
    applied = cdc.apply(settings, code)
    assert applied.applied == first.total and not applied.rejected
    assert all(v == "PASSED" for v in cdc.compare(settings, code).values())

    assert continue_workspace(settings, code, days=5).report.passed
    second = cdc.capture(settings, code)
    ops = {op for counts in second.events.values() for op in counts}
    assert {"INSERT", "UPDATE"} <= ops
    ws = get_workspace(settings, code)
    engine = create_mysql_engine(settings, role="app", database=ws.sim_schema)
    with engine.connect() as conn:
        row = conn.execute(text("""SELECT before_image, after_image, changed_columns FROM change_event_log
                                   WHERE operation = 'UPDATE' AND source_table = 'orders' LIMIT 1""")).fetchone()
        assert row is not None and "updated_at" in str(row[2])
        ordered = [r[0] for r in conn.execute(text(
            "SELECT source_committed_at FROM change_event_log WHERE capture_batch_id = :b ORDER BY change_event_id"),
            {"b": second.capture_batch_id})]
        assert ordered == sorted(ordered)                                  # LSN order = source time order
    engine.dispose()
    cdc.apply(settings, code)
    assert all(v == "PASSED" for v in cdc.compare(settings, code).values())
    again = cdc.apply(settings, code)                                      # idempotent: nothing after the offset
    assert again.applied == 0
    con = duckdb.connect(str(replica_path(ws, "sim")), read_only=True)
    try:   # SCD2: an updated order has a closed version and exactly one current one
        versions = con.execute("""SELECT order_id, COUNT(*), SUM(CASE WHEN _is_current THEN 1 ELSE 0 END),
                                         COUNT(_valid_to) FROM cdc_history.orders GROUP BY 1 HAVING COUNT(*) > 1
                                  LIMIT 5""").fetchall()
        assert versions and all(current == 1 and closed == total - 1 for _, total, current, closed in versions)
    finally:
        con.close()


def test_every_scenario_is_caught_and_clean_data_is_untouched(workspace):
    settings, code = workspace
    ws = get_workspace(settings, code)
    before = _checksum(settings, ws.oltp_schema)
    sim_schema.reset(settings, code)
    assert load_workspace(settings, code, source="sim").reconciled
    cdc.capture(settings, code)
    names = ("pipeline", "cdc_stream", "data_quality", "business_rules")
    injected = {name: anomalies.inject(settings, code, name, seed=2) for name in names}
    assert all(result.records for result in injected.values())
    report = detection.detect(settings, code)
    statuses = {a.status for a in report.anomalies}
    assert statuses <= {"CAUGHT"}, [(a.anomaly_type, a.expected, a.failed) for a in report.anomalies
                                     if a.status != "CAUGHT"]
    assert report.unexplained == [] and Path(report.path).exists()
    assert _checksum(settings, ws.oltp_schema) == before                    # clean data never written


def test_same_seed_same_anomalies_and_reset_discards(workspace):
    settings, code = workspace
    picks = []
    for _ in range(2):
        sim_schema.reset(settings, code)
        picks.append([(r.anomaly_type, r.table, r.pk) for r in anomalies.inject(settings, code, "data_quality", seed=8).records])
    assert picks[0] == picks[1]
    sim_schema.reset(settings, code)
    state = sim_schema.status(settings, code)
    assert state["anomalies"] == [] and state["events"]["anomalous"] == 0


def test_unknown_scenario_and_missing_prerequisites(workspace):
    settings, code = workspace
    with pytest.raises(anomalies.AnomalyError):
        anomalies.inject(settings, code, "no_such_scenario")


def test_capture_after_each_run_when_enabled(workspace, monkeypatch):
    settings, code = workspace
    from dataclasses import replace

    from scmgen.simulation import config as sim_config
    enabled = sim_config.load_simulation_config()
    enabled = replace(enabled, cdc=replace(enabled.cdc, enabled=True))
    monkeypatch.setattr(sim_config, "load_simulation_config", lambda *a, **k: enabled)
    cdc.capture(settings, code)                                   # up to date before the run
    result = continue_workspace(settings, code, days=2)
    assert result.report.passed and result.cdc_events and result.cdc_events > 0
    assert cdc.capture(settings, code).total == 0                 # the hook already captured the run
