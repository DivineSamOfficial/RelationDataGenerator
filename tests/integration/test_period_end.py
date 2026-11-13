"""Phase 3 step 3.14c: month-end depreciation (P23), period balances rebuilt at close (D54), journal completeness
(V-AC-12) and period balances (V-AC-13), and demand-driven replenishment, on generated data.

Window: January–April 2025, Small preset (January to March close inside it). Every rule involved must pass, and each
mutation (rolled back) must make its rule fail.
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
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-04-30"}
RULES = ("V-AC-12", "V-AC-13")


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zp" + uuid.uuid4().hex[:6]
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


def test_depreciation(generated):
    _, conn = generated
    rows = _rows(conn, """SELECT document_date, total_debit, journal_type FROM journal_entries
                          WHERE journal_source = 'DEPRECIATION' ORDER BY document_date""")
    assert [r[0].month for r in rows] == [1, 2, 3, 4] and all(r[0].day >= 28 and r[2] == "RECURRING" for r in rows)
    # Straight line: the same charge every month (cost × yearly rate / 12 per asset account).
    assert len({r[1] for r in rows}) == 1 and rows[0][1] > 0


def test_period_balances(generated):
    _, conn = generated
    closed = _rows(conn, """SELECT p.period_code, COUNT(*), SUM(b.closing_balance),
                                   MIN(b.rebuilt_at) > (SELECT MAX(j.created_at) FROM journal_entries j
                                                        WHERE j.accounting_period_id = p.accounting_period_id)
                            FROM accounting_periods p JOIN gl_period_balances b USING (accounting_period_id)
                            GROUP BY p.accounting_period_id, p.period_code ORDER BY 1""")
    accounts = _one(conn, "SELECT COUNT(*) FROM chart_of_accounts WHERE is_postable")
    assert [c[0] for c in closed] == ["2025-01", "2025-02", "2025-03"]
    assert all(c[1] == accounts and c[2] == 0 and c[3] for c in closed)
    # The trial balance of each closed period equals the cumulative journal lines up to its end.
    assert _one(conn, """SELECT COUNT(*) FROM gl_period_balances b JOIN accounting_periods p USING (accounting_period_id)
                         JOIN chart_of_accounts a USING (account_id) JOIN account_types t USING (account_type_id)
                         WHERE t.financial_statement = 'BALANCE_SHEET' AND b.closing_balance <> (
                             SELECT COALESCE(SUM(l.debit_amount - l.credit_amount), 0) FROM journal_entry_lines l
                             JOIN journal_entries j USING (journal_entry_id) JOIN accounting_periods q
                               ON q.accounting_period_id = j.accounting_period_id
                             WHERE l.account_id = b.account_id AND q.end_date <= p.end_date)""") == 0


def test_replenishment_follows_demand(generated):
    _, conn = generated
    # Expiry losses stay a small share of sales (they were larger than sales before demand-driven replenishment).
    expired = -_one(conn, """SELECT COALESCE(SUM(l.value_change), 0) FROM stock_adjustment_line_items l
                             JOIN stock_adjustments a USING (stock_adjustment_id) WHERE a.adjustment_type = 'EXPIRY_WRITE_OFF'""")
    sales = _one(conn, "SELECT SUM(taxable_amount) FROM invoices WHERE invoice_status = 'ISSUED'")
    assert expired < sales * Decimal("0.05")
    # Purchases stay in proportion to what sold (cost of sales), not to the master-data reorder levels.
    bought = _one(conn, "SELECT SUM(value_change) FROM inventory_transactions WHERE transaction_type = 'PURCHASE_RECEIPT'")
    issued = -_one(conn, "SELECT SUM(value_change) FROM inventory_transactions WHERE transaction_type = 'SALES_ISSUE'")
    assert bought < issued * 2


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_INVOICE_JOURNAL = _FIRST.format(key="journal_entry_id", table="journal_entries", where="journal_source = 'SALES_INVOICE'")
_BALANCE = _FIRST.format(key="gl_period_balance_id", table="gl_period_balances", where="period_debit > 0")

MUTATIONS = {
    "V-AC-12": [f"DELETE FROM journal_entry_lines WHERE journal_entry_id = {_INVOICE_JOURNAL}",
                f"DELETE FROM journal_entries WHERE journal_entry_id = {_INVOICE_JOURNAL}"],
    "V-AC-13": [f"UPDATE gl_period_balances SET period_debit = period_debit + 1, closing_balance = closing_balance + 1 "
                f"WHERE gl_period_balance_id = {_BALANCE}"],
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
