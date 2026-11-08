"""Runs against the real local MySQL configured in .env (admin login)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine, verify_connection
from scmgen.db.server_check import evaluate_server, fetch_server_info

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admin_engine():
    settings = load_settings()
    engine = create_mysql_engine(settings, role="admin")
    yield settings, engine
    engine.dispose()


def test_admin_can_connect(admin_engine) -> None:
    _, engine = admin_engine
    verify_connection(engine)


def test_session_runs_in_ist(admin_engine) -> None:
    _, engine = admin_engine
    with engine.connect() as conn:
        assert conn.execute(text("SELECT @@SESSION.time_zone")).scalar() == "+05:30"


def test_server_meets_all_blocking_requirements(admin_engine) -> None:
    settings, engine = admin_engine
    mysql = settings.mysql
    results = evaluate_server(
        fetch_server_info(engine), mysql.min_server_version, mysql.charset, mysql.collation, mysql.session_time_zone
    )
    failures = [f"{r.name}: {r.detail}" for r in results if r.blocking and not r.passed]
    assert failures == []
