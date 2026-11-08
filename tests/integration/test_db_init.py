"""`scmgen db init` against the real local MySQL: accounts, privilege matrix, registry constraints.

Creates throwaway schemas scm_itest_oltp / scm_itest_sim (dropped afterwards). Registry
inserts run inside transactions that are always rolled back.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from scmgen.core.settings import DEFAULT_ENV_FILE, SERVICE_ROLES, load_settings
from scmgen.db.bootstrap import init_database
from scmgen.db.connection import create_mysql_engine

pytestmark = pytest.mark.integration

ACCESS_DENIED_DB = 1044
ACCESS_DENIED_TABLE = 1142
CHECK_VIOLATED = 3819
DUPLICATE_KEY = 1062

OLTP = "scm_itest_oltp"
SIM = "scm_itest_sim"


def _error_code(exc: DBAPIError) -> int:
    return exc.orig.args[0]


def _expect_error(engine: Engine, sql: str, code: int, params: dict | None = None) -> None:
    with engine.connect() as conn, pytest.raises(DBAPIError) as info:
        conn.execute(text(sql), params or {})
    assert _error_code(info.value) == code, info.value


@pytest.fixture(scope="module")
def engines():
    first = init_database(load_settings(), DEFAULT_ENV_FILE)
    second = init_database(load_settings(), DEFAULT_ENV_FILE)  # must be idempotent
    settings = load_settings()  # re-read: init may have written passwords to .env
    created = {role: create_mysql_engine(settings, role=role) for role in SERVICE_ROLES}

    with created["app"].connect() as conn:  # app itself creates the workspace-style schemas
        for schema in (OLTP, SIM):
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{schema}`")
            conn.exec_driver_sql(f"CREATE DATABASE `{schema}`")
            conn.exec_driver_sql(f"CREATE TABLE `{schema}`.t (id INT PRIMARY KEY, v VARCHAR(10))")
            conn.exec_driver_sql(f"INSERT INTO `{schema}`.t VALUES (1, 'clean')")
        conn.commit()

    yield {"settings": settings, "first": first, "second": second, **created}

    with created["app"].connect() as conn:
        for schema in (OLTP, SIM):
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{schema}`")
    for engine in created.values():
        engine.dispose()


def test_second_init_changes_nothing(engines) -> None:
    second = engines["second"].actions
    assert not any("generated password" in a for a in second)
    assert sum("password re-applied" in a for a in second) == 3


# --- app: everything inside scm_%, nothing outside -------------------------------------

def test_app_cannot_create_schema_outside_prefix(engines) -> None:
    _expect_error(engines["app"], "CREATE DATABASE itest_outside_prefix", ACCESS_DENIED_DB)


def test_app_can_write_control_registry(engines) -> None:
    with engines["app"].connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM scm_control.workspaces")).scalar_one() >= 0


# --- reader: read-only everywhere under scm_% ------------------------------------------

def test_reader_can_read(engines) -> None:
    with engines["reader"].connect() as conn:
        assert conn.execute(text(f"SELECT v FROM `{OLTP}`.t")).scalar_one() == "clean"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (f"INSERT INTO `{OLTP}`.t VALUES (2, 'x')", ACCESS_DENIED_TABLE),
        (f"UPDATE `{SIM}`.t SET v = 'x'", ACCESS_DENIED_TABLE),
        ("CREATE DATABASE scm_itest2_oltp", ACCESS_DENIED_DB),
    ],
)
def test_reader_cannot_write(engines, sql: str, code: int) -> None:
    _expect_error(engines["reader"], sql, code)


# --- anomaly: read clean, write only simulation (D73) ---------------------------------

def test_anomaly_reads_clean_and_control(engines) -> None:
    with engines["anomaly"].connect() as conn:
        assert conn.execute(text(f"SELECT v FROM `{OLTP}`.t")).scalar_one() == "clean"
        conn.execute(text("SELECT COUNT(*) FROM scm_control.workspaces")).scalar_one()


@pytest.mark.parametrize(
    "sql",
    [
        f"INSERT INTO `{OLTP}`.t VALUES (2, 'dirty')",
        f"UPDATE `{OLTP}`.t SET v = 'dirty'",
        f"DELETE FROM `{OLTP}`.t",
        "INSERT INTO scm_control.workspaces (workspace_code) VALUES ('x')",
    ],
)
def test_anomaly_cannot_modify_clean_or_control(engines, sql: str) -> None:
    _expect_error(engines["anomaly"], sql, ACCESS_DENIED_TABLE)


def test_anomaly_can_modify_simulation_schema(engines) -> None:
    with engines["anomaly"].connect() as conn:
        conn.exec_driver_sql(f"CREATE TABLE `{SIM}`.anomaly_probe (id INT)")
        conn.exec_driver_sql(f"UPDATE `{SIM}`.t SET v = 'dirty'")
        conn.exec_driver_sql(f"DROP TABLE `{SIM}`.anomaly_probe")
        conn.commit()
    with engines["app"].connect() as conn:  # clean copy untouched
        assert conn.execute(text(f"SELECT v FROM `{OLTP}`.t")).scalar_one() == "clean"


# --- scm_control.workspaces constraints -----------------------------------------------

def _row(**overrides) -> dict:
    row = {
        "code": "abc",
        "company": "Fictional Traders Pvt Ltd",
        "industries": json.dumps(["beauty"]),
        "oltp": "scm_abc_oltp",
        "sim": "scm_abc_sim",
        "duck": "data/duckdb/abc.duckdb",
        "duck_sim": "data/duckdb/abc_sim.duckdb",
        "seed": 42,
        "build": str(uuid.uuid4()),
        "config": json.dumps({"company": "x"}),
        "status": "READY",
    }
    row.update(overrides)
    return row


INSERT_WORKSPACE = text(
    "INSERT INTO scm_control.workspaces (workspace_code, company_name, industries, oltp_schema, sim_schema, "
    "duckdb_path, duckdb_sim_path, base_seed, build_id, config_snapshot, workspace_status) VALUES "
    "(:code, :company, :industries, :oltp, :sim, :duck, :duck_sim, :seed, :build, :config, :status)"
)


def test_valid_workspace_row_is_accepted_and_timestamps_default(engines) -> None:
    with engines["app"].connect() as conn:
        conn.execute(INSERT_WORKSPACE, _row())
        created, updated = conn.execute(
            text("SELECT created_at, updated_at FROM scm_control.workspaces WHERE workspace_code = 'abc'")
        ).one()
        assert created is not None and created == updated
        conn.rollback()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"code": "ABC"}, CHECK_VIOLATED),             # upper case (case-sensitive REGEXP)
        ({"code": "a"}, CHECK_VIOLATED),               # too short
        ({"code": "1abc"}, CHECK_VIOLATED),            # must start with a letter
        ({"code": "ab_c"}, CHECK_VIOLATED),            # no underscores
        ({"status": "BOGUS"}, CHECK_VIOLATED),
        ({"industries": json.dumps({"x": 1})}, CHECK_VIOLATED),   # must be an array
        ({"industries": json.dumps([])}, CHECK_VIOLATED),         # at least one industry
        ({"build": "not-a-uuid"}, CHECK_VIOLATED),
        ({"sim": "scm_abc_oltp"}, CHECK_VIOLATED),     # schemas must differ
    ],
)
def test_invalid_workspace_rows_are_rejected(engines, overrides: dict, code: int) -> None:
    with engines["app"].connect() as conn:
        with pytest.raises(DBAPIError) as info:
            conn.execute(INSERT_WORKSPACE, _row(**overrides))
        conn.rollback()
    assert _error_code(info.value) == code


def test_duplicate_workspace_code_is_rejected(engines) -> None:
    with engines["app"].connect() as conn:
        conn.execute(INSERT_WORKSPACE, _row())
        with pytest.raises(DBAPIError) as info:
            conn.execute(INSERT_WORKSPACE, _row(oltp="scm_abd_oltp", sim="scm_abd_sim"))
        conn.rollback()
    assert _error_code(info.value) == DUPLICATE_KEY
