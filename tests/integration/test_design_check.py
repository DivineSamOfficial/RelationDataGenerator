"""Phase 2 step 2.7: the live schema matches the design docs, and drift in a workspace is caught.

The drift test uses a throwaway workspace (code prefix 'zd') that is always removed afterwards.
"""

from __future__ import annotations

import secrets
import string

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from scmgen.cli.main import app
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.design_check import SCRATCH_CODE, run_design_check
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def settings():
    return load_settings()


def _schema_exists(settings, schema: str) -> bool:
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        found = conn.execute(text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = :s"),
                             {"s": schema}).scalar_one()
    engine.dispose()
    return found == 1


def test_packaged_ddl_and_control_registry_match_the_design(settings):
    scopes = run_design_check(settings)
    assert [(s.tables, s.views) for s in scopes] == [(92, 1), (1, 0), (5, 0)]   # + the _sim control tables
    for scope in scopes:
        assert scope.differences == [], "\n".join(d.describe() for d in scope.differences)
    assert not _schema_exists(settings, f"scm_{SCRATCH_CODE}_oltp")      # scratch schemas dropped
    assert not _schema_exists(settings, f"scm_{SCRATCH_CODE}_sim")


@pytest.fixture
def workspace(settings):
    code = "zd" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    try:
        create_workspace(settings, code, "Drift Test Traders", ["beauty"])
        yield code
    finally:
        engine = create_mysql_engine(settings, role="app")
        with engine.connect() as conn:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
            conn.commit()
        engine.dispose()


def test_drift_in_a_live_workspace_is_reported(settings, workspace):
    schema = f"scm_{workspace}_oltp"
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        for statement in (
            f"ALTER TABLE `{schema}`.customers ADD COLUMN loyalty_tier VARCHAR(10) NULL",   # not in design
            f"ALTER TABLE `{schema}`.product_brands MODIFY brand_name VARCHAR(50) NOT NULL",  # type changed
            f"ALTER TABLE `{schema}`.shipping_carriers DROP COLUMN tracking_url_template",  # column removed
            f"CREATE TABLE `{schema}`.scratch_notes (note_id INT PRIMARY KEY)",              # table not in design
        ):
            execute_raw(conn, statement)
    engine.dispose()

    workspace_scope, control_scope = run_design_check(settings, workspace=workspace)
    found = {(d.kind, d.table, d.column) for d in workspace_scope.differences}
    assert found == {
        ("COLUMN_NOT_IN_DESIGN", "customers", "loyalty_tier"),
        ("TYPE", "product_brands", "brand_name"),
        ("COLUMN_MISSING_IN_DB", "shipping_carriers", "tracking_url_template"),
        ("TABLE_NOT_IN_DESIGN", "scratch_notes", None),
    }
    assert control_scope.differences == []

    result = CliRunner().invoke(app, ["db", "check-design", "-w", workspace])
    assert result.exit_code == 1
    assert "4 difference(s)" in result.output and "loyalty_tier" in result.output


def test_cli_reports_a_clean_match(settings):
    result = CliRunner().invoke(app, ["db", "check-design"])
    assert result.exit_code == 0, result.output
    assert "Schema matches the design." in result.output
