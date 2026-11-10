"""Phase 3 step 3.6: the daily engine (D64-D68) on throwaway workspaces (prefix 'ze').

The headline guarantees: one transaction per day (a failed day leaves no trace and the run resumes
cleanly), and the same seed gives the same database however the history is split into runs (D65).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import text

import scmgen.generation.engine as engine_module
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace, get_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 10)
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-11-30"}   # includes 22 Sep 2025
SYSTEM_TABLES = {"generation_runs", "generation_run_steps", "document_sequences", "companies"}


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def workspaces(settings):
    created: list[str] = []

    def make(seed: int = 42) -> str:
        code = "ze" + uuid.uuid4().hex[:6]
        create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty"], base_seed=seed)
        created.append(code)
        return code

    yield make
    engine = create_mysql_engine(settings, role="app")
    with engine.connect() as conn:
        for code in created:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
        conn.commit()
    engine.dispose()
    for code in created:
        folder = settings.data_dir / "reports" / code
        for path in folder.glob("*.json"):
            path.unlink()
        if folder.exists():
            folder.rmdir()


def _config(**overrides):
    return load_generation_config(preset="small", today=TODAY, overrides={**WINDOW, **overrides})


def _query(settings, code: str, sql: str):
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).all()
    engine.dispose()
    return rows


def _checksums(settings, code: str) -> dict[str, int]:
    """A checksum per business table over every column except ``generation_run_id``: which run wrote a row
    is run metadata and legitimately depends on how the history was split (D65 covers the business data)."""
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
            sums[table] = conn.exec_driver_sql(
                f"SELECT COUNT(*), COALESCE(SUM(CRC32(CONCAT_WS('|', {row}))), 0) FROM `{table}`").one()
    engine.dispose()
    return sums


def test_history_runs_day_by_day_and_validates(settings, workspaces):
    code = workspaces()
    result = generate_history(settings, code, _config())
    assert result.status == "COMPLETED" and result.days == 122 and result.report.passed
    assert (result.first_day, result.last_day) == (date(2025, 8, 1), date(2025, 11, 30))
    assert result.master_data_rows > 0 and result.workspace_status == "READY"
    runs = _query(settings, code, "SELECT run_number, sim_start_date, sim_end_date, committed_through, run_status "
                                  "FROM generation_runs ORDER BY run_number")
    assert [(r[1], r[2], r[4]) for r in runs] == [(date(2025, 7, 31), date(2025, 7, 31), "COMPLETED"),
                                                  (date(2025, 8, 1), date(2025, 11, 30), "COMPLETED")]
    assert str(get_workspace(settings, code).last_committed_through) == "2025-11-30 23:59:59.999999"
    # Periods: open on the 1st, closed on the 7th of the next month (Sunday 7 Sep → Monday 8 Sep).
    periods = dict(_query(settings, code, "SELECT period_code, CONCAT(period_status, ' ', IFNULL(DATE(closed_at), '-')) "
                                          "FROM accounting_periods WHERE period_code BETWEEN '2025-08' AND '2025-11'"))
    assert periods == {"2025-08": "CLOSED 2025-09-08", "2025-09": "CLOSED 2025-10-07",
                       "2025-10": "CLOSED 2025-11-07", "2025-11": "OPEN -"}
    # GST revision on its day: products then at 28% (monitors, TVs, ACs) now have an 18% row from 22 Sep 2025.
    revised = _query(settings, code, "SELECT gst_rate, effective_from, COUNT(*) FROM product_tax_rates "
                                     "WHERE change_reason = 'GST_RATE_REVISION' GROUP BY 1, 2")
    # Electronics 28% → 18% (monitors, TVs, ACs); beauty 18% → 5% (shampoo, soap, toothpaste …), all on 22 Sep 2025.
    assert {(float(r[0]), r[1]) for r in revised} == {(18.0, date(2025, 9, 22)), (5.0, date(2025, 9, 22))}
    assert _query(settings, code, "SELECT COUNT(*) FROM product_tax_rates WHERE gst_rate = 28 AND effective_to IS NULL")[0][0] == 0
    # Opening position on the first simulated day, once: P01 + one OPENING_STOCK adjustment (and journal) per warehouse.
    opening = _query(settings, code, "SELECT journal_source, journal_type, COUNT(*), MIN(posting_date) FROM journal_entries "
                                     "GROUP BY 1, 2 ORDER BY 1")
    counts = {(r[0], r[1]): r[2] for r in opening}
    assert counts[("OPENING_BALANCE", "OPENING")] == 1 and counts[("STOCK_ADJUSTMENT", "SYSTEM")] == 8
    assert all(r[3] == date(2025, 8, 1) for r in opening if r[0] != "GOODS_RECEIPT")
    assert counts[("GOODS_RECEIPT", "SYSTEM")] > 0                     # receipts from 3.7b
    adjustments = _query(settings, code, "SELECT COUNT(*), COUNT(DISTINCT warehouse_id), MIN(adjustment_type) "
                                         "FROM stock_adjustments")[0]
    assert adjustments == (8, 8, "OPENING_STOCK")
    steps = dict(_query(settings, code, "SELECT step_name, step_status FROM generation_run_steps s JOIN generation_runs r "
                                        "USING (generation_run_id) WHERE r.run_number = 2"))
    assert steps["OPENING_BALANCES"] == "COMPLETED" and steps["DAILY_SIMULATION"] == "COMPLETED"
    # Batch-tracked (beauty) opening stock carries batches that haven't expired on day one.
    assert _query(settings, code, "SELECT COUNT(*) FROM inventory_batches WHERE expiry_date <= '2025-08-01'")[0][0] == 0
    # Campaigns move with the clock; the next ones are created 30 days ahead.
    statuses = dict(_query(settings, code, "SELECT campaign_status, COUNT(*) FROM marketing_campaigns GROUP BY 1"))
    assert statuses.get("COMPLETED", 0) > 0 and statuses.get("PLANNED", 0) > 0
    assert _query(settings, code, "SELECT COUNT(*) FROM marketing_campaigns WHERE campaign_status = 'PLANNED' "
                                  "AND start_date <= '2025-11-30'")[0][0] == 0


def test_same_history_whether_run_at_once_or_in_parts(settings, workspaces):
    """D65: day-keyed random streams + state reloaded from the database ⇒ identical data."""
    whole, parts = workspaces(), workspaces()
    generate_history(settings, whole, _config())
    for days in (25, 40, 200):
        generate_history(settings, parts, _config(), days=days)
    assert _checksums(settings, whole) == _checksums(settings, parts)
    assert len(_query(settings, parts, "SELECT * FROM generation_runs")) == 4


def test_a_failing_day_rolls_back_alone_and_the_run_resumes(settings, workspaces, monkeypatch):
    """D64: the failed day leaves nothing behind; the next run continues from the last committed day and
    ends with exactly the data of an uninterrupted run."""
    reference, interrupted = workspaces(), workspaces()
    generate_history(settings, reference, _config())

    def explode(day):
        if day.day == date(2025, 9, 10):
            raise RuntimeError("simulated crash")
    monkeypatch.setattr(engine_module, "DAY_STEPS", engine_module.DAY_STEPS + (("explode", explode),))
    with pytest.raises(RuntimeError, match="simulated crash"):
        generate_history(settings, interrupted, _config())
    run = _query(settings, interrupted, "SELECT run_status, committed_through, error_message FROM generation_runs "
                                        "WHERE run_number = 2")[0]
    assert run[0] == "FAILED" and run[1] == datetime(2025, 9, 9, 23, 59, 59, 999999) and "2025-09-10" in run[2]
    assert get_workspace(settings, interrupted).status == "READY"
    monkeypatch.undo()
    result = generate_history(settings, interrupted, _config())
    assert result.first_day == date(2025, 9, 10) and result.status == "COMPLETED"
    assert _checksums(settings, interrupted) == _checksums(settings, reference)


def test_cancel_takes_effect_between_days(settings, workspaces, monkeypatch):
    code = workspaces()

    def request_cancel(day):
        if day.day == date(2025, 8, 15):
            day.conn.execute(text("UPDATE generation_runs SET cancel_requested = TRUE WHERE generation_run_id = :r"),
                             {"r": day.run_id})
    monkeypatch.setattr(engine_module, "DAY_STEPS", engine_module.DAY_STEPS + (("cancel", request_cancel),))
    result = generate_history(settings, code, _config())
    assert result.status == "CANCELLED" and result.last_day == date(2025, 8, 15) and result.report.passed
    run = _query(settings, code, "SELECT run_status, committed_through FROM generation_runs WHERE run_number = 2")[0]
    assert run == ("CANCELLED", datetime(2025, 8, 15, 23, 59, 59, 999999))
    monkeypatch.undo()
    assert generate_history(settings, code, _config()).first_day == date(2025, 8, 16)


def test_nothing_to_do_when_up_to_date(settings, workspaces):
    code = workspaces()
    generate_history(settings, code, _config())
    again = generate_history(settings, code, _config())
    assert again.status == "NOTHING_TO_DO" and again.days == 0
