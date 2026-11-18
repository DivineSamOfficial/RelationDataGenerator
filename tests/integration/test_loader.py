"""Phase 4: the generic MySQL → DuckDB loader (1G §6-7, D69-D71): first full load, incremental APPEND / MERGE after
Continue, idempotent re-runs, a table reload, a full reload after a rebuild (A2), the validation gate (D76),
reconciliation and the loader rules V-LD-01 / V-LD-02 (each with a mutation that makes it fail)."""

from __future__ import annotations

import uuid
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace, get_workspace, set_workspace_status
from scmgen.generation.actions import continue_workspace, rebuild_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.loader.config import load_loader_config
from scmgen.loader.run import LoaderError, load_status, load_workspace
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-02-28"}


@pytest.fixture(scope="module")
def workspace():
    settings = load_settings()
    code = "zl" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "fmcg"], base_seed=3)
    try:
        assert generate_history(settings, code, load_generation_config(preset="small", overrides=WINDOW)).report.passed
        yield settings, code
    finally:
        path = Path(get_workspace(settings, code).duckdb_path)
        admin = create_mysql_engine(settings, role="app")
        with admin.connect() as conn:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
            conn.commit()
        admin.dispose()
        for file in (path, path.with_suffix(".duckdb.wal")):
            if file.exists():
                file.unlink()
        folder = settings.data_dir / "reports" / code
        for report in folder.glob("*.json"):
            report.unlink()
        if folder.exists():
            folder.rmdir()


def _replica(settings, code):
    return duckdb.connect(str(get_workspace(settings, code).duckdb_path), read_only=True)


def _source_count(settings, code, table):
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        count = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
    engine.dispose()
    return count


def test_first_load_is_full_and_reconciled(workspace):
    settings, code = workspace
    result = load_workspace(settings, code)
    assert result.full_reason == "first load" and result.status == "SUCCESS" and result.reconciled
    assert len(result.tables) == 92 and all(t.load_type == "FULL" for t in result.tables)
    con = _replica(settings, code)
    try:
        assert con.execute('SELECT COUNT(*) FROM raw."orders"').fetchone()[0] == _source_count(settings, code, "orders")
        # Exact types: money stays DECIMAL, timestamps keep microseconds (1G §6.2).
        types = dict(con.execute("""SELECT column_name, data_type FROM information_schema.columns
                                    WHERE table_schema = 'raw' AND table_name = 'invoices'""").fetchall())
        assert types["total_amount"] == "DECIMAL(18,2)" and types["issued_at"] == "TIMESTAMP"
        marks = dict(con.execute("SELECT strategy, COUNT(*) FROM etl_meta.etl_table_watermarks GROUP BY 1").fetchall())
        assert marks == {"FULL": 24, "APPEND": 21, "MERGE": 47}
    finally:
        con.close()


def test_incremental_after_continue_and_idempotent_rerun(workspace):
    settings, code = workspace
    assert continue_workspace(settings, code, days=7).report.passed
    result = load_workspace(settings, code)
    by_type = {t.load_type for t in result.tables}
    assert result.full_reason is None and by_type == {"FULL", "INCREMENTAL_APPEND", "INCREMENTAL_MERGE"}
    assert result.reconciled and sum(t.rows_inserted for t in result.tables if t.load_type != "FULL") > 0
    assert sum(t.rows_updated for t in result.tables if t.strategy == "MERGE") > 0
    again = load_workspace(settings, code)                   # same window: nothing new to move
    assert all(t.rows_extracted == 0 for t in again.tables if t.strategy != "FULL") and again.reconciled
    con = _replica(settings, code)
    try:
        for table in ("orders", "inventory_transactions", "journal_entry_lines"):
            assert con.execute(f'SELECT COUNT(*) FROM raw."{table}"').fetchone()[0] == _source_count(settings, code, table)
    finally:
        con.close()


def test_table_reload(workspace):
    settings, code = workspace
    result = load_workspace(settings, code, tables=["orders"])
    assert [(t.table, t.load_type) for t in result.tables] == [("orders", "TABLE_RELOAD")] and result.reconciled


def test_validation_gate(workspace):
    settings, code = workspace
    set_workspace_status(settings, code, "NEEDS_ATTENTION")
    try:
        with pytest.raises(LoaderError):
            load_workspace(settings, code)
        assert load_workspace(settings, code, force=True).status == "SUCCESS"
    finally:
        set_workspace_status(settings, code, "READY")


def test_loader_rules_and_mutations(workspace):
    settings, code = workspace
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        report = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=["V-LD-01", "V-LD-02"])
        assert {r.rule_id: r.status for r in report.results} == {"V-LD-01": "PASSED", "V-LD-02": "PASSED"}
        try:                       # an APPEND row updated after insert breaks V-LD-02
            conn.execute(text("UPDATE inventory_transactions SET updated_at = updated_at + INTERVAL 1 SECOND LIMIT 1"))
            result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=["V-LD-02"]).results[0]
            assert result.status == "FAILED"
        finally:
            conn.rollback()
    engine.dispose()
    path = get_workspace(settings, code).duckdb_path
    con = duckdb.connect(str(path))
    con.execute("BEGIN TRANSACTION")
    con.execute('DELETE FROM raw."orders" WHERE order_id = (SELECT MIN(order_id) FROM raw."orders")')
    con.execute("COMMIT")
    con.close()
    with engine.connect() as conn:  # a row missing from the replica breaks V-LD-01; a reload repairs it
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=["V-LD-01"]).results[0]
        assert result.status == "FAILED"
    assert load_workspace(settings, code, tables=["orders"]).reconciled
    engine.dispose()


def test_replica_held_by_another_program(workspace):
    """A SQL client holding the replica (owner report 2026-11-18, DBeaver): validation skips the replica check
    instead of flagging the clean data NEEDS_ATTENTION; a load stops with a clear message naming the holder."""
    import subprocess
    import sys
    from scmgen.loader.replica import ReplicaLockedError
    settings, code = workspace
    path = get_workspace(settings, code).duckdb_path
    hold = "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); print('held', flush=True); time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", hold, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
        with engine.connect() as conn:
            report = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=["V-LD-01"])
        engine.dispose()
        result = report.results[0]
        assert result.status == "NOT_APPLICABLE" and "is open in" in result.message and report.passed
        with pytest.raises(ReplicaLockedError, match="is open in"):
            load_workspace(settings, code)
    finally:
        proc.kill()
        proc.wait()
    assert load_workspace(settings, code).reconciled                  # free again: loads normally


def test_rebuild_triggers_a_full_reload(workspace):
    settings, code = workspace
    rebuild_workspace(settings, code, confirm=code)
    result = load_workspace(settings, code)
    assert result.full_reason.startswith("source rebuilt") and all(t.load_type == "FULL" for t in result.tables)
    assert result.reconciled and load_status(settings, code)["build_matches"]


def test_loader_config_covers_every_table(workspace):
    settings, code = workspace
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        tables = {t for (t,) in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"))}
    engine.dispose()
    assert set(load_loader_config().strategies) == tables
