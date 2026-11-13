"""Phase 3 step 3.16: Continue, Rebuild and Plan (D14, D68, D76) and the determinism proof V-SY-04: a rebuilt
workspace (same seed and settings) has exactly the same data as the original, table by table."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace, get_workspace, set_workspace_status
from scmgen.generation.actions import continue_workspace, last_run, plan, rebuild_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history

pytestmark = pytest.mark.integration

WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-02-15"}
SYSTEM_TABLES = {"generation_runs", "generation_run_steps", "document_sequences", "companies", "change_event_log"}
# (run bookkeeping, as in test_engine.py: number series rows and the company row carry wall-clock times, D74)


def _checksums(settings, code: str) -> dict[str, tuple]:
    """Per business table: row count and a CRC of every column except ``generation_run_id`` (run metadata)."""
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    sums = {}
    with engine.connect() as conn:
        columns: dict[str, list[str]] = {}
        for table, column in conn.execute(text(
                "SELECT c.TABLE_NAME, c.COLUMN_NAME FROM information_schema.COLUMNS c JOIN information_schema.TABLES t "
                "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME WHERE c.TABLE_SCHEMA = DATABASE() "
                "AND t.TABLE_TYPE = 'BASE TABLE' ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION")):
            if table not in SYSTEM_TABLES and column != "generation_run_id":
                columns.setdefault(table, []).append(column)
        for table, cols in sorted(columns.items()):
            row = ", ".join(f"COALESCE(CAST(`{c}` AS CHAR), '<null>')" for c in cols)
            sums[table] = tuple(conn.exec_driver_sql(
                f"SELECT COUNT(*), COALESCE(SUM(CRC32(CONCAT_WS('|', {row}))), 0) FROM `{table}`").one())
    engine.dispose()
    return sums


@pytest.fixture(scope="module")
def workspace():
    settings = load_settings()
    code = "zq" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty"], base_seed=5)
    try:
        config = load_generation_config(preset="small", overrides=WINDOW)
        assert generate_history(settings, code, config).report.passed
        yield settings, code
    finally:
        admin = create_mysql_engine(settings, role="app")
        with admin.connect() as conn:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
            conn.commit()
        admin.dispose()
        folder = settings.data_dir / "reports" / code
        for path in folder.glob("*.json"):
            path.unlink()
        if folder.exists():
            folder.rmdir()


def test_plan_and_continue(workspace):
    settings, code = workspace
    run = last_run(settings, code)
    assert run.preset == "small" and run.start_date == date(2025, 1, 1) and run.committed_through == date(2025, 2, 15)
    preview = plan(settings, code, days=10)
    assert (preview.first_day, preview.last_day, preview.days, preview.blocked) == \
        (date(2025, 2, 16), date(2025, 2, 25), 10, None)
    # Continue keeps the original settings and start date and moves the end forward (D68).
    result = continue_workspace(settings, code, days=10)
    assert result.status == "COMPLETED" and (result.first_day, result.last_day) == (date(2025, 2, 16), date(2025, 2, 25))
    assert result.report.passed
    assert last_run(settings, code).start_date == date(2025, 1, 1)


def test_continue_refused_after_failed_validation(workspace):
    settings, code = workspace
    set_workspace_status(settings, code, "NEEDS_ATTENTION")
    try:
        assert plan(settings, code, days=1).blocked
        with pytest.raises(WorkspaceError):
            continue_workspace(settings, code, days=1)
    finally:
        set_workspace_status(settings, code, "READY")


def test_rebuild_reproduces_the_data(workspace):
    """V-SY-04: the same seed and settings give the same data (here: a history built in two runs, rebuilt in one)."""
    settings, code = workspace
    before = _checksums(settings, code)
    build = get_workspace(settings, code).build_id
    with pytest.raises(WorkspaceError):
        rebuild_workspace(settings, code, confirm="wrong")
    result = rebuild_workspace(settings, code, confirm=code)
    assert result.report.passed and result.last_day == date(2025, 2, 25)
    assert get_workspace(settings, code).build_id != build
    after = _checksums(settings, code)
    assert {t for t in before if before[t] != after[t]} == set()
