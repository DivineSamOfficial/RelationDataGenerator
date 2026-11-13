"""Phase 3 step 3.14a: bank statement lines for every money movement, gateway settlements and COD remittances
(P10, D53, D63), clawbacks of settled receipts, bank charges and interest (P21), petty-cash floats and treasury
transfers (INTERNAL_TRANSFER pairs), and monthly bank reconciliations, on generated data.

Window: January–April 2025, Small preset. Every bank rule must pass, and each mutation (rolled back) must make its
rule fail.
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
RULES = ("V-BK-01", "V-BK-02")


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zb" + uuid.uuid4().hex[:6]
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


def test_settlements_clear_the_clearing_account(generated):
    _, conn = generated
    # D53/D63: each clearing receipt is settled by one bank line on its settlement date; Payment Clearing holds
    # exactly the receipts not settled yet.
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN bank_transactions t
                         ON t.bank_transaction_id = p.settlement_bank_transaction_id
                         WHERE t.statement_date <> p.settlement_date""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                         WHERE p.payment_status = 'SUCCESS' AND p.settlement_date <= '2025-04-30'
                           AND (m.method_code IN ('UPI', 'CARD', 'NET_BANKING') OR p.payment_type = 'COD_COLLECTION')
                           AND p.settlement_bank_transaction_id IS NULL""") == 0
    clearing = _one(conn, """SELECT SUM(l.debit_amount - l.credit_amount) FROM journal_entry_lines l
                             JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'PAYMENT_CLEARING'""")
    unsettled = _one(conn, """SELECT COALESCE(SUM(p.amount), 0) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                              WHERE p.payment_status = 'SUCCESS' AND p.settlement_bank_transaction_id IS NULL
                                AND (m.method_code IN ('UPI', 'CARD', 'NET_BANKING') OR p.payment_type = 'COD_COLLECTION')""")
    assert clearing == unsettled
    # Gateway fees are booked at settlement (P10), on the Finance cost centre.
    fees = _one(conn, "SELECT SUM(gateway_fee_amount) FROM customer_payments WHERE settlement_bank_transaction_id IS NOT NULL")
    booked = _one(conn, """SELECT SUM(l.debit_amount) FROM journal_entry_lines l JOIN chart_of_accounts a USING (account_id)
                           JOIN journal_entries j USING (journal_entry_id)
                           WHERE a.system_role = 'PAYMENT_GATEWAY_CHARGES' AND j.journal_source = 'BANK_SETTLEMENT'""")
    assert fees > 0 and booked == fees
    # A settled receipt reversed later (chargeback, refund of a cancelled order) is clawed back once.
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p WHERE p.payment_status = 'REVERSED'
                         AND p.settlement_bank_transaction_id IS NOT NULL
                         AND (SELECT COUNT(*) FROM bank_transactions t WHERE t.customer_payment_id = p.customer_payment_id
                                AND t.transaction_type = 'REVERSAL') <> 1""") == 0


def test_balances_and_treasury(generated):
    _, conn = generated
    # Current accounts and floats never go overdrawn (treasury funds PAYMENTS, floats are topped up); only the
    # cash-credit account may be drawn.
    assert _one(conn, """SELECT COUNT(*) FROM bank_transactions t JOIN bank_accounts b USING (bank_account_id)
                         WHERE b.account_type <> 'CASH_CREDIT' AND t.balance_after < 0""") == 0
    kinds = dict(_rows(conn, "SELECT transaction_type, COUNT(*) FROM bank_transactions GROUP BY 1"))
    assert {"GATEWAY_SETTLEMENT", "COD_REMITTANCE", "SUPPLIER_PAYMENT", "EXPENSE_PAYMENT", "INTERNAL_TRANSFER",
            "BANK_CHARGES", "CUSTOMER_RECEIPT", "REFUND"} <= set(kinds)
    assert kinds["INTERNAL_TRANSFER"] % 2 == 0
    # Charges on the last working day of each month per current / cash-credit account; interest each quarter end.
    assert _one(conn, "SELECT COUNT(DISTINCT MONTH(statement_date)) FROM bank_transactions WHERE transaction_type = 'BANK_CHARGES'") == 4
    assert _one(conn, "SELECT COUNT(*) FROM bank_transactions WHERE transaction_type = 'INTEREST_CREDIT'") == 1


def test_reconciliations(generated):
    _, conn = generated
    rows = _rows(conn, """SELECT p.period_code, COUNT(*), SUM(r.reconciliation_status = 'RECONCILED'), MIN(r.reconciled_at)
                          FROM bank_reconciliations r JOIN accounting_periods p USING (accounting_period_id)
                          GROUP BY 1 ORDER BY 1""")
    accounts = _one(conn, "SELECT COUNT(*) FROM bank_accounts")
    assert [r[0] for r in rows] == ["2025-01", "2025-02", "2025-03"] and all(r[1] == r[2] == accounts for r in rows)
    # Reconciled in the first days of the next month, before that period closes.
    assert _one(conn, """SELECT COUNT(*) FROM bank_reconciliations r JOIN accounting_periods p USING (accounting_period_id)
                         WHERE r.reconciled_at > p.closed_at OR DATE(r.reconciled_at) <= p.end_date""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM bank_transactions WHERE statement_date < '2025-04-01'
                         AND bank_reconciliation_id IS NULL""") == 0


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_LINE = _FIRST.format(key="bank_transaction_id", table="bank_transactions", where="transaction_type = 'SUPPLIER_PAYMENT'")
_TRANSFER = _FIRST.format(key="bank_transaction_id", table="bank_transactions", where="transaction_type = 'INTERNAL_TRANSFER'")
_SETTLEMENT = _FIRST.format(key="bank_transaction_id", table="bank_transactions", where="transaction_type = 'GATEWAY_SETTLEMENT'")
_RECONCILIATION = _FIRST.format(key="bank_reconciliation_id", table="bank_reconciliations", where="TRUE")

MUTATIONS = {
    "V-BK-01": [f"UPDATE bank_transactions SET balance_after = balance_after + 1 WHERE bank_transaction_id = {_LINE}"],
    "V-BK-01/document": [f"DELETE FROM bank_transactions WHERE bank_transaction_id = {_LINE}"],
    "V-BK-01/transfer": [f"UPDATE bank_transactions SET counter_bank_transaction_id = NULL WHERE bank_transaction_id = {_TRANSFER}"],
    "V-BK-01/settlement": [f"UPDATE customer_payments SET settlement_bank_transaction_id = NULL "
                           f"WHERE settlement_bank_transaction_id = {_SETTLEMENT} LIMIT 1"],
    "V-BK-02": [f"UPDATE bank_reconciliations SET reconciliation_status = 'EXCEPTION', reconciled_at = NULL, "
                f"reconciled_by_employee_id = NULL WHERE bank_reconciliation_id = {_RECONCILIATION}"],
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
