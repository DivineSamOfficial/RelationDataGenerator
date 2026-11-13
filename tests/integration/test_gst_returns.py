"""Phase 3 step 3.14b: GST returns per GSTIN and month — GSTR-1 sections and HSN summary, GSTR-3B with the legal
set-off (D58), the settlement journal (P22) and the tax payment, on generated data.

Window: January–April 2025, Small preset, with late filing raised so that it occurs. Every rule involved must
pass, and each mutation (rolled back) must make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-04-30", "gst.late_share": 0.3}
RULES = ("V-TX-03",)


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zt" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty", "fmcg"],
                     base_seed=7)
    try:
        config = load_generation_config(preset="small", today=date(2026, 11, 10), overrides=WINDOW)
        result = generate_history(settings, code, config)
        assert result.report.passed, [r.rule_id for r in result.report.results if r.status not in ("PASSED", "PENDING")]
        engine = create_mysql_engine(settings, role="app", database=f"scm_{code}_oltp")
        with engine.connect() as conn:
            yield code, conn
        engine.dispose()
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


def _rows(conn, sql):
    return conn.execute(text(sql)).all()


def _one(conn, sql):
    return conn.execute(text(sql)).scalar()


def test_monthly_returns(generated):
    _, conn = generated
    rows = _rows(conn, """SELECT company_gst_registration_id, return_period, return_type, filed_date, due_date, return_status
                          FROM gst_returns ORDER BY 1, 2, 3""")
    gstins = _one(conn, "SELECT COUNT(*) FROM company_gst_registrations")
    assert {(r[1], r[2]) for r in rows} == {(p, t) for p in ("2025-01", "2025-02", "2025-03") for t in ("GSTR1", "GSTR3B")}
    assert len(rows) <= 6 * gstins
    by_return = {(r[0], r[1], r[2]): r for r in rows}
    for (gstin, period, kind), r in by_return.items():
        assert r[4].day == (11 if kind == "GSTR1" else 20) and (r[5] == "FILED_LATE") == (r[3] > r[4])
        if kind == "GSTR3B" and (gstin, period, "GSTR1") in by_return:
            assert by_return[(gstin, period, "GSTR1")][3] <= r[3]        # GSTR-1 comes first
    assert any(r[5] == "FILED_LATE" for r in rows)
    # Every tax row of a filed period is in exactly one return of its direction.
    assert _one(conn, """SELECT COUNT(*) FROM tax_transactions t LEFT JOIN gst_returns r USING (gst_return_id)
                         WHERE t.return_period <= '2025-03'
                           AND (r.gst_return_id IS NULL
                                OR r.return_type <> IF(t.tax_direction = 'OUTPUT', 'GSTR1', 'GSTR3B'))""") == 0


def test_settlement_and_payment(generated):
    _, conn = generated
    # P22 per 3B with a liability: Dr Output = liability, Cr Input = credit used, Cr Bank = cash; GST_PAYMENT line.
    assert _one(conn, """SELECT COUNT(*) FROM gst_returns r LEFT JOIN journal_entries j USING (gst_return_id)
                         WHERE r.return_type = 'GSTR3B' AND r.output_cgst + r.output_sgst + r.output_igst > 0
                           AND (j.journal_entry_id IS NULL OR j.journal_source <> 'GST_SETTLEMENT'
                                OR j.total_debit <> r.output_cgst + r.output_sgst + r.output_igst)""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM gst_returns r LEFT JOIN bank_transactions b USING (gst_return_id)
                         WHERE r.cash_paid_cgst + r.cash_paid_sgst + r.cash_paid_igst > 0
                           AND (b.bank_transaction_id IS NULL OR b.statement_date <> r.filed_date)""") == 0
    # After filing, each GSTIN's output accounts hold only the periods not settled yet (here April).
    open_output = _one(conn, """SELECT COALESCE(SUM(l.credit_amount - l.debit_amount), 0) FROM journal_entry_lines l
                                JOIN tax_accounts a ON a.gl_account_id = l.account_id WHERE a.tax_direction = 'OUTPUT'""")
    april = _one(conn, """SELECT COALESCE(SUM(cgst_amount + sgst_amount + igst_amount), 0) FROM tax_transactions
                          WHERE tax_direction = 'OUTPUT' AND gst_return_id IS NULL""")
    assert open_output == april


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_SECTION = _FIRST.format(key="gst_return_line_item_id", table="gst_return_line_items", where="section_code = 'B2CS'")
_IGST_USE = _FIRST.format(key="gst_return_id", table="gst_returns",
                          where="return_type = 'GSTR3B' AND itc_used_igst >= 1")
_PAID = _FIRST.format(key="gst_return_line_item_id", table="gst_return_line_items",
                      where="section_code = '6.1' AND paid_through = 'ITC_IGST' AND igst_amount >= 1")

MUTATIONS = {
    "V-TX-03": [f"UPDATE gst_return_line_items SET igst_amount = igst_amount + 1 WHERE gst_return_line_item_id = {_SECTION}"],
    "V-TX-03/set-off": [f"UPDATE gst_returns SET itc_used_igst = itc_used_igst - 1, cash_paid_igst = cash_paid_igst + 1, "
                        f"payment_date = COALESCE(payment_date, filed_date), bank_account_id = COALESCE(bank_account_id, "
                        f"(SELECT MIN(bank_account_id) FROM bank_accounts)), challan_reference = COALESCE(challan_reference, 'X') "
                        f"WHERE gst_return_id = {_IGST_USE}"],
    "V-TX-03/6.1": [f"UPDATE gst_return_line_items SET igst_amount = igst_amount - 1 WHERE gst_return_line_item_id = {_PAID}"],
}


def test_every_rule_has_a_mutation():
    assert {key.split("/")[0] for key in MUTATIONS} >= set(RULES)


@pytest.mark.parametrize("key", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, key):
    code, conn = generated
    rule_id = key.split("/")[0]
    try:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for statement in MUTATIONS[key]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
