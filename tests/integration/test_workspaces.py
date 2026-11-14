"""Phase 2 step 2.5: workspace lifecycle against the real local MySQL.

Each test uses a random throwaway code (prefix 'zt') and always removes its schemas and registry row.
"""

from __future__ import annotations

import json
import secrets
import string

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import scmgen.db.workspaces as workspaces_module
from scmgen.cli.main import app
from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import (
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    workspace_lock,
)

pytestmark = pytest.mark.integration

INDUSTRIES = ["beauty", "electronics"]


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def code(settings):
    """A unique throwaway workspace code, fully cleaned up afterwards whatever the test did."""
    value = "zt" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    yield value
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        for suffix in ("oltp", "sim"):
            execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{value}_{suffix}`")
        conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": value})
        conn.commit()
    engine.dispose()


def _schema_exists(settings, schema: str) -> bool:
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema}
        ).scalar_one()
    engine.dispose()
    return bool(found)


def test_create_builds_full_schema_and_registers(settings, code) -> None:
    info = create_workspace(settings, code, "Fictional Traders Pvt Ltd", INDUSTRIES, base_seed=42)
    assert info.status == "READY"
    assert info.oltp_tables == 92          # every business table from the design
    assert info.sim_tables == 0            # simulation schema stays empty until Phase 7
    assert info.base_seed == 42 and info.industries == INDUSTRIES

    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        views = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s AND table_type = 'VIEW'"
        ), {"s": f"scm_{code}_oltp"}).scalar_one()
        snapshot = json.loads(conn.execute(text(
            "SELECT config_snapshot FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code}).scalar_one())
    engine.dispose()
    assert views == 1
    assert snapshot["industries"] == INDUSTRIES and len(snapshot["ddl_sha256"]) == 64


def test_random_seed_is_generated_and_recorded(settings, code) -> None:
    info = create_workspace(settings, code, "Seedless Co", ["beauty"])
    assert get_workspace(settings, code).base_seed == info.base_seed


def test_duplicate_create_is_refused_and_original_untouched(settings, code) -> None:
    first = create_workspace(settings, code, "First Co", ["beauty"])
    with pytest.raises(WorkspaceError, match="already exists"):
        create_workspace(settings, code, "Second Co", ["beauty"])
    again = get_workspace(settings, code)
    assert (again.company_name, again.build_id, again.oltp_tables) == ("First Co", first.build_id, 92)


def test_unregistered_schema_with_same_name_is_never_overwritten(settings, code) -> None:
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        execute_raw(conn, f"CREATE DATABASE `scm_{code}_oltp`")
        execute_raw(conn, f"CREATE TABLE `scm_{code}_oltp`.precious (id INT)")
    engine.dispose()
    with pytest.raises(WorkspaceError, match="isn't registered"):
        create_workspace(settings, code, "Clash Co", ["beauty"])
    assert _schema_exists(settings, f"scm_{code}_oltp")  # left exactly as it was
    with pytest.raises(WorkspaceError, match="No workspace"):
        get_workspace(settings, code)


def test_failed_create_is_rolled_back_completely(settings, code, monkeypatch) -> None:
    def boom(conn, schema):
        raise RuntimeError("simulated DDL failure")

    monkeypatch.setattr(workspaces_module, "apply_workspace_ddl", boom)
    with pytest.raises(WorkspaceError, match="rolled back"):
        create_workspace(settings, code, "Doomed Co", ["beauty"])
    assert not _schema_exists(settings, f"scm_{code}_oltp")
    assert not _schema_exists(settings, f"scm_{code}_sim")
    with pytest.raises(WorkspaceError, match="No workspace"):
        get_workspace(settings, code)


def test_busy_workspace_is_refused(settings, code) -> None:
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as holder, workspace_lock(holder, code):  # another "operation" holds the lock
        with pytest.raises(WorkspaceError, match="busy"):
            create_workspace(settings, code, "Racing Co", ["beauty"])
    engine.dispose()


def test_delete_archives_then_purge_frees_code(settings, code) -> None:
    create_workspace(settings, code, "Short Lived Co", ["beauty"])
    assert delete_workspace(settings, code) == "ARCHIVED"
    archived = get_workspace(settings, code)
    assert archived.status == "ARCHIVED"
    assert archived.oltp_tables is None and archived.sim_tables is None  # schemas gone
    with pytest.raises(WorkspaceError, match="already exists"):
        create_workspace(settings, code, "Reuse Too Early", ["beauty"])
    with pytest.raises(WorkspaceError, match="already archived"):
        delete_workspace(settings, code)
    assert delete_workspace(settings, code, purge=True) == "PURGED"
    assert create_workspace(settings, code, "Reborn Co", ["beauty"]).status == "READY"


def test_workspace_left_generating_by_a_killed_run_can_be_deleted(settings, code) -> None:
    from scmgen.db.workspaces import set_workspace_status
    create_workspace(settings, code, "Crashed Run Co", ["beauty"])
    set_workspace_status(settings, code, "GENERATING")        # the run died without resetting its status
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as holder, workspace_lock(holder, code):  # while a run is alive: refused
        with pytest.raises(WorkspaceError, match="busy"):
            delete_workspace(settings, code, purge=True)
    engine.dispose()
    assert delete_workspace(settings, code, purge=True) == "PURGED"   # lock free: the run is dead


def test_cli_delete_needs_exactly_one_of_workspace_or_all() -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["workspace", "delete"]).exit_code == 2
    assert runner.invoke(app, ["workspace", "delete", "-w", "abc", "--all"]).exit_code == 2


def test_cli_delete_all_requires_typing_all(code) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["workspace", "create", "--code", code, "--company", "All Co",
                               "--industries", "beauty"]).exit_code == 0
    refused = runner.invoke(app, ["workspace", "delete", "--all", "--purge"], input="yes\n")
    assert refused.exit_code == 1 and "Nothing was deleted" in refused.output
    assert code in refused.output                              # the prompt lists what would go


def test_list_includes_workspace_with_health(settings, code) -> None:
    create_workspace(settings, code, "Listed Co", ["beauty"])
    listed = {ws.code: ws for ws in list_workspaces(settings)}
    assert listed[code].oltp_tables == 92


def test_cli_lifecycle_and_confirmation(code) -> None:
    runner = CliRunner()
    created = runner.invoke(app, ["workspace", "create", "--code", code, "--company", "Cli Co", "--industries", "beauty"])
    assert created.exit_code == 0, created.output
    assert "92 tables" in created.output

    listed = runner.invoke(app, ["workspace", "list"])
    assert listed.exit_code == 0 and code in listed.output

    shown = runner.invoke(app, ["workspace", "show", "--workspace", code])
    assert shown.exit_code == 0 and "no data generated yet" in shown.output

    refused = runner.invoke(app, ["workspace", "delete", "--workspace", code], input="wrongcode\n")
    assert refused.exit_code == 1 and "Nothing was deleted" in refused.output

    deleted = runner.invoke(app, ["workspace", "delete", "--workspace", code, "--purge"], input=f"{code}\n")
    assert deleted.exit_code == 0 and "PURGED" in deleted.output


def test_cli_rejects_bad_code_before_touching_database() -> None:
    result = CliRunner().invoke(app, ["workspace", "create", "--code", "Bad_Code", "--company", "X", "--industries", "beauty"])
    assert result.exit_code == 2
    assert "Invalid workspace code" in result.output
