"""Phase 3 step 3.5: master data is generated on day 0, passes validation, and is reproducible.

Workspaces use throwaway codes (prefix 'zm') and fixed history dates, and are always removed.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from scmgen.core.errors import ScmgenError, WorkspaceError
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace, get_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.master import generate_master_data

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 10)
DATES = {"history.start_date": "2026-04-01", "history.end_date": "2026-06-30"}
SYSTEM_TABLES = {"generation_runs", "generation_run_steps", "document_sequences"}


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def workspaces(settings):
    """Factory for throwaway workspaces; everything is dropped afterwards."""
    created: list[str] = []

    def make(industries=("electronics", "home_kitchen"), seed=42, company="Kaveri Nexus Distribution Pvt. Ltd."):
        code = "zm" + uuid.uuid4().hex[:6]
        create_workspace(settings, code, company, list(industries), base_seed=seed)
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
        for path in (settings.data_dir / "reports" / code).glob("*.json"):
            path.unlink()
        (settings.data_dir / "reports" / code).rmdir() if (settings.data_dir / "reports" / code).exists() else None


def _config(**overrides):
    return load_generation_config(preset="small", today=TODAY, overrides={**DATES, **overrides})


def _checksums(settings, code: str) -> dict[str, int]:
    schema = f"scm_{code}_oltp"
    engine = create_mysql_engine(settings, role="app", database=schema)
    with engine.connect() as conn:
        tables = [t for (t,) in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :s AND TABLE_TYPE = 'BASE TABLE'"),
            {"s": schema}) if t not in SYSTEM_TABLES]
        sums = {t: conn.exec_driver_sql(f"CHECKSUM TABLE `{t}`").one()[1] for t in sorted(tables)}
    engine.dispose()
    return sums


def test_master_data_is_generated_on_day_zero_and_validates(settings, workspaces):
    code = workspaces()
    result = generate_master_data(settings, code, _config())
    assert result.setup_day == "2026-03-31" and result.report.passed and result.workspace_status == "READY"
    rows = result.rows_by_table
    assert rows["companies"] == 1 and rows["warehouses"] == 8 and rows["company_gst_registrations"] == 5
    assert rows["business_units"] == 2 and rows["tax_accounts"] == 30 and rows["accounting_periods"] == 12
    assert rows["bank_accounts"] == 3 + 8 and rows["cost_centers"] == 1 + 2 + 8 + 10
    assert rows["products"] == 400 and rows["customers"] == 240 + 1160 and rows["shipping_carriers"] == 6
    assert rows["product_brands"] == 23 and rows["supplier_products"] >= 2 * 400
    statuses = {r.rule_id: r.status for r in result.report.results}
    # Every master-data rule is checked on real rows now; only V-MD-06 (document approvers) waits for 3.7.
    assert {rid: st for rid, st in statuses.items() if rid.startswith("V-MD") and st != "PASSED"} == {}
    ws = get_workspace(settings, code)
    assert ws.status == "READY" and str(ws.last_committed_through) == "2026-03-31 23:59:59.999999"
    engine = create_mysql_engine(settings, role="app", database=ws.oltp_schema)
    with engine.connect() as conn:
        run = conn.execute(text("SELECT run_status, sim_start_date, sim_end_date, validation_status, rows_written "
                                "FROM generation_runs")).one()
        assert run[:4] == ("COMPLETED", date(2026, 3, 31), date(2026, 3, 31), "PASSED") and run[4] == sum(rows.values())
        assert conn.execute(text("SELECT MAX(updated_at) FROM chart_of_accounts")).scalar_one().date() == date(2026, 3, 31)
        # Story consistency: the principal GSTIN comes first; every state registers before its first warehouse opens.
        late = conn.execute(text("""
            SELECT COUNT(*) FROM company_gst_registrations r
            WHERE r.registration_date > (SELECT MIN(opened_date) FROM warehouses w
                                         WHERE w.company_gst_registration_id = r.company_gst_registration_id)""")).scalar_one()
        assert late == 0
        assert conn.execute(text("SELECT COUNT(*) FROM employees e JOIN warehouses w USING (warehouse_id) "
                                 "WHERE e.hire_date < w.opened_date - INTERVAL 30 DAY")).scalar_one() == 0
        periods = conn.execute(text("SELECT MIN(start_date), MAX(end_date), COUNT(DISTINCT period_status) "
                                    "FROM accounting_periods")).one()
        assert periods == (date(2026, 4, 1), date(2027, 3, 31), 1)
    engine.dispose()


def test_same_seed_gives_identical_master_data(settings, workspaces):
    """D20 + D65: ids, values and simulated timestamps are all reproducible, table by table."""
    first, second, other = workspaces(seed=7), workspaces(seed=7), workspaces(seed=8)
    for code in (first, second, other):
        generate_master_data(settings, code, _config())
    a, b, c = _checksums(settings, first), _checksums(settings, second), _checksums(settings, other)
    # The company code is the workspace code, so the companies table differs by design; all else matches.
    differing = {t for t in a if a[t] != b[t]}
    assert differing == {"companies"}
    assert {t for t in a if a[t] != c[t]} >= {"employees", "warehouses", "company_gst_registrations"}


def test_presets_scale_the_organisation(settings, workspaces):
    small, medium = workspaces(), workspaces()
    small_rows = generate_master_data(settings, small, _config()).rows_by_table
    medium_rows = generate_master_data(settings, medium, load_generation_config(
        preset="medium", today=TODAY, overrides=DATES)).rows_by_table
    assert 36 <= small_rows["employees"] <= 45                     # structural minimum for 5 DCs + 3 stores
    assert 170 <= medium_rows["employees"] <= 195                  # design: ~180
    assert medium_rows["warehouse_locations"] == 5 * 400 + 3 * 60


def test_second_master_data_run_is_refused(settings, workspaces):
    code = workspaces()
    generate_master_data(settings, code, _config())
    with pytest.raises(WorkspaceError, match="already has master data"):
        generate_master_data(settings, code, _config())


def test_impossible_setup_fails_cleanly(settings, workspaces):
    code = workspaces()
    with pytest.raises(ScmgenError, match="principal place"):
        generate_master_data(settings, code, _config(**{"setup.hq_city": "Jaipur"}))
    ws = get_workspace(settings, code)
    assert ws.status == "READY"
    engine = create_mysql_engine(settings, role="app", database=ws.oltp_schema)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM companies")).scalar_one() == 0
        assert conn.execute(text("SELECT run_status FROM generation_runs")).scalar_one() == "FAILED"
    engine.dispose()


def _rates(settings, code: str) -> dict[str, list[tuple]]:
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.category_code, t.gst_rate, t.change_reason, t.effective_to IS NULL AS current_rate "
            "FROM product_tax_rates t JOIN products p USING (product_id) "
            "JOIN product_categories c ON c.product_category_id = p.product_category_id")).all()
    engine.dispose()
    by_category: dict[str, list[tuple]] = {}
    for category, gst_rate, reason, current in rows:
        by_category.setdefault(category, []).append((float(gst_rate), reason, bool(current)))
    return by_category


@pytest.mark.parametrize("dates, revision, expected_monitor_rates", [
    # Day 0 after 22 Sep 2025: older monitors carry 28% INITIAL (closed) + 18% REVISION (current).
    ({"history.start_date": "2026-04-01", "history.end_date": "2026-06-30"}, True, {28.0, 18.0}),
    # Day 0 before the revision: one open 28% row; the daily engine applies the change on the day.
    ({"history.start_date": "2025-04-01", "history.end_date": "2025-06-30"}, True, {28.0}),
    # Revision switched off (D17): rates stay at the pre-revision slab.
    ({"history.start_date": "2026-04-01", "history.end_date": "2026-06-30"}, False, {28.0}),
])
def test_gst_rate_history_follows_the_revision_date(settings, workspaces, dates, revision, expected_monitor_rates):
    code = workspaces(industries=("electronics",))
    generate_master_data(settings, code, load_generation_config(
        preset="small", today=TODAY, overrides={**dates, "gst_rate_revision": revision}))
    monitors = _rates(settings, code)["ELEC-CMP-MON"]
    assert {rate for rate, _, _ in monitors} == expected_monitor_rates
    assert all(rate == 18.0 for rate, _, current in monitors if current) == (expected_monitor_rates == {28.0, 18.0})
    earbuds = _rates(settings, code)["ELEC-AUD-EAR"]                 # 18% before and after: never revised
    assert {reason for _, reason, _ in earbuds} == {"INITIAL"}


def test_a_b2b_only_company_has_no_consumers(settings, workspaces):
    code = workspaces(industries=("industrial_mro",))
    rows = generate_master_data(settings, code, _config()).rows_by_table
    assert rows["customers"] == 240
    engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM customers WHERE customer_type = 'B2C'")).scalar_one() == 0
    engine.dispose()
