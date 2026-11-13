"""Phase 5: the Streamlit control panel renders every page against a generated workspace without an exception,
every curated example query of the Table guide runs, the guide covers every table, and background jobs work."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from streamlit.testing.v1 import AppTest

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import create_workspace, delete_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen import __version__
from scmgen.ui import jobs
from scmgen.ui.guide import load_examples, load_table_docs

PAGES = ("Workspaces", "Run monitor", "Data explorer", "Validation", "Scenarios", "DuckDB load", "Table guide", "Reset")

pytestmark = pytest.mark.integration
APP = str(Path(__file__).resolve().parents[2] / "src" / "scmgen" / "ui" / "app.py")


@pytest.fixture(scope="module")
def workspace():
    settings = load_settings()
    code = "zu" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "fmcg"], base_seed=5)
    try:
        config = load_generation_config(preset="small", overrides={"history.start_date": "2025-01-01",
                                                                    "history.end_date": "2025-01-31"})
        assert generate_history(settings, code, config).report.passed
        yield settings, code
    finally:
        delete_workspace(settings, code, purge=True)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(workspace, page):
    _, code = workspace
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    at.sidebar.selectbox[0].set_value(code)
    at.sidebar.radio[0].set_value(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_guide_covers_every_table_and_examples_run(workspace):
    settings, code = workspace
    docs = load_table_docs()
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    try:
        with engine.connect() as conn:
            tables = {t for (t,) in conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))}
            assert tables - set(docs) == set()          # every table and view has a design-doc section
            for table, examples in load_examples().items():
                assert table in tables, table
                for example in examples:
                    conn.execute(text(f"SELECT * FROM ({example['sql'].strip().rstrip(';')}) q LIMIT 5")).fetchall()
    finally:
        engine.dispose()


def test_background_job_runs_the_cli():
    job = jobs.start("version", ["version"])
    deadline = time.time() + 60
    while job.exit_code is None and time.time() < deadline:
        time.sleep(0.5)
    assert job.exit_code == 0 and job.tail().strip() == __version__
    assert any(j.job_id == job.job_id for j in jobs.recent())
