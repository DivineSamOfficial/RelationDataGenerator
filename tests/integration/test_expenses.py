"""Phase 3 step 3.13: expenses and payroll — recurring site and company bills, usage bills (freight V-SH-05,
marketplace commission, campaign media), ad-hoc bills, travel claims, petty cash, monthly payroll (D56), approval
with P19 and AP items (D57), the weekly payment run (P20), on generated data.

Window: January–April 2025, Small preset. Every expense rule must pass, and each mutation (rolled back) must make
its rule fail.
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
WINDOW = {"history.start_date": "2025-01-01", "history.end_date": "2025-04-30", "expenses.rejection_share": 0.3}
RULES = ("V-EX-01", "V-EX-02", "V-EX-03", "V-SH-05")


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "ze" + uuid.uuid4().hex[:6]
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


def test_recurring_bills(generated):
    _, conn = generated
    # One bill per recurrence key and service period; rent for every site every month (billed on the 1st).
    assert _one(conn, """SELECT COUNT(*) FROM (SELECT recurrence_key, service_period_start FROM expenses
                         WHERE recurrence_key IS NOT NULL GROUP BY 1, 2 HAVING COUNT(*) > 1) x""") == 0
    rents = _rows(conn, """SELECT service_period_start, COUNT(*), MIN(expense_date) FROM expenses
                           WHERE recurrence_key LIKE 'RENT-%' GROUP BY 1 ORDER BY 1""")
    sites = _one(conn, "SELECT COUNT(*) FROM warehouses")
    assert [r[0].month for r in rents] == [1, 2, 3, 4] and all(r[1] == sites and r[2] == r[0] for r in rents)
    # Usage bills for last month arrive in the first days of the next; electricity follows the season.
    assert _one(conn, """SELECT COUNT(*) FROM expenses WHERE recurrence_key LIKE 'ELECTRICITY-%'
                         AND expense_date <= service_period_end""") == 0
    assert _one(conn, "SELECT COUNT(*) FROM expenses WHERE recurrence_key LIKE 'FREIGHT-%'") > 0
    assert _one(conn, "SELECT COUNT(*) FROM expenses WHERE recurrence_key = 'MARKETPLACE-FEES'") > 0
    # The annual insurance bill in April.
    assert _one(conn, "SELECT COUNT(*) FROM expenses WHERE recurrence_key = 'INSURANCE-2025'") == 1


def test_approval_posting_and_ap(generated):
    _, conn = generated
    statuses = dict(_rows(conn, "SELECT expense_status, COUNT(*) FROM expenses GROUP BY 1"))
    assert statuses["APPROVED"] > 100 and statuses.get("REJECTED", 0) > 0
    assert _one(conn, """SELECT COUNT(*) FROM expenses WHERE expense_status = 'REJECTED'
                         AND (rejection_reason IS NULL OR expense_type NOT IN ('VENDOR_BILL', 'EMPLOYEE_CLAIM'))""") == 0
    # P19: Cr AP = total for every approved non-payroll expense; ineligible GST expensed, eligible to Input GST.
    assert _one(conn, """SELECT COUNT(*) FROM expenses e JOIN journal_entries j ON j.expense_id = e.expense_id
                         JOIN journal_entry_lines l ON l.journal_entry_id = j.journal_entry_id AND l.credit_amount > 0
                         JOIN chart_of_accounts a ON a.account_id = l.account_id
                         WHERE e.expense_type <> 'PAYROLL' AND a.system_role = 'AP_CONTROL'
                           AND l.credit_amount <> e.total_amount""") == 0
    rows = dict(_rows(conn, "SELECT itc_eligibility, COUNT(*) FROM tax_transactions WHERE document_type = 'EXPENSE' GROUP BY 1"))
    assert rows.get("ELIGIBLE", 0) > 0
    # Unregistered suppliers (utilities, petty-cash shops, some landlords) charge no GST.
    assert _one(conn, """SELECT COUNT(*) FROM expenses e JOIN suppliers s USING (supplier_id)
                         WHERE s.gst_registration_type <> 'REGISTERED' AND e.cgst_amount + e.sgst_amount + e.igst_amount > 0""") == 0
    # Attachments on ~95% of non-payroll expenses, fictional storage only.
    share = _one(conn, """SELECT AVG(EXISTS (SELECT 1 FROM expense_attachments_metadata m WHERE m.expense_id = e.expense_id))
                          FROM expenses e WHERE e.expense_type <> 'PAYROLL'""")
    assert Decimal("0.85") < share <= 1


def test_payments(generated):
    _, conn = generated
    # Weekly run on Thursdays for bills and claims; petty cash the same day in cash from the site's float.
    runs = {r[0] for r in _rows(conn, """SELECT DISTINCT WEEKDAY(p.payment_date) FROM expense_payments p
                                         JOIN expenses e USING (expense_id) WHERE e.expense_type IN ('VENDOR_BILL', 'EMPLOYEE_CLAIM')""")}
    assert runs == {3}
    assert _one(conn, """SELECT COUNT(*) FROM expense_payments p JOIN expenses e USING (expense_id)
                         WHERE e.expense_type = 'PETTY_CASH' AND (p.payment_date <> e.expense_date OR p.amount > 10000)""") == 0
    # Nothing paid twice; a failed payment is retried later.
    assert _one(conn, """SELECT COUNT(*) FROM (SELECT expense_id FROM expense_payments WHERE payment_status = 'COMPLETED'
                         GROUP BY expense_id HAVING SUM(amount) > MAX((SELECT total_amount FROM expenses x
                         WHERE x.expense_id = expense_payments.expense_id))) y""") == 0


def test_payroll(generated):
    _, conn = generated
    payroll = _rows(conn, """SELECT expense_id, expense_date, total_amount, due_date FROM expenses
                             WHERE expense_type = 'PAYROLL' ORDER BY expense_date""")
    assert [p[1].month for p in payroll] == [1, 2, 3, 4]
    assert all(p[1].day >= 28 for p in payroll)
    salaries = _one(conn, "SELECT SUM(monthly_gross_salary) FROM employees WHERE employment_status = 'ACTIVE'")
    assert all(p[2] == salaries.quantize(Decimal(1), ROUND_HALF_UP) for p in payroll)   # full months, whole rupees
    # Paid on the first working day after the month end; the last month's payroll is still payable.
    paid = dict(_rows(conn, """SELECT expense_id, MIN(payment_date) FROM expense_payments
                               WHERE payment_status = 'COMPLETED' GROUP BY 1"""))
    assert all(paid[p[0]] == p[3] for p in payroll[:-1]) and payroll[-1][0] not in paid
    payable = _one(conn, """SELECT SUM(l.credit_amount - l.debit_amount) FROM journal_entry_lines l
                            JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'SALARIES_PAYABLE'""")
    assert payable == payroll[-1][2]


_FIRST = "(SELECT {key} FROM (SELECT MIN({key}) AS {key} FROM {table} WHERE {where}) x)"
_PETTY_LINE = _FIRST.format(key="expense_line_item_id", table="expense_line_items",
                            where="expense_id IN (SELECT expense_id FROM expenses WHERE expense_type = 'PETTY_CASH')")
_RENT_LINE = _FIRST.format(key="expense_line_item_id", table="expense_line_items",
                           where="expense_id IN (SELECT expense_id FROM expenses WHERE recurrence_key LIKE 'RENT-%')")
_PETTY_PAYMENT = _FIRST.format(key="expense_payment_id", table="expense_payments",
                               where="expense_id IN (SELECT expense_id FROM expenses WHERE expense_type = 'PETTY_CASH')")
_FREIGHT_SHIPMENT = _FIRST.format(key="shipment_id", table="shipments",
                                  where="freight_cost_amount > 0 AND dispatched_at < '2025-03-01'")
_BILL_PAYMENT = _FIRST.format(key="expense_payment_id", table="expense_payments",
                              where="payment_status = 'COMPLETED' AND expense_id IN (SELECT expense_id FROM expenses "
                                    "WHERE expense_type = 'VENDOR_BILL')")
_EXPENSE_TAX = _FIRST.format(key="tax_transaction_id", table="tax_transactions", where="document_type = 'EXPENSE'")

MUTATIONS = {
    "V-EX-01": [f"UPDATE expense_line_items SET taxable_amount = taxable_amount + 1, line_total = line_total + 1 "
                f"WHERE expense_line_item_id = {_PETTY_LINE}"],
    "V-EX-02": [f"UPDATE expense_line_items SET cost_center_id = (SELECT c.cost_center_id FROM cost_centers c "
                f"JOIN departments d USING (department_id) WHERE d.department_code = 'FIN') "
                f"WHERE expense_line_item_id = {_RENT_LINE}"],
    "V-EX-03": [f"UPDATE expense_payments SET bank_account_id = (SELECT MIN(bank_account_id) FROM bank_accounts "
                f"WHERE account_purpose = 'PAYMENTS') WHERE expense_payment_id = {_PETTY_PAYMENT}"],
    "V-SH-05": [f"UPDATE shipments SET freight_cost_amount = freight_cost_amount + 1 WHERE shipment_id = {_FREIGHT_SHIPMENT}"],
    "V-AP-02/expense": [f"DELETE FROM journal_entry_lines WHERE journal_entry_id IN (SELECT journal_entry_id FROM journal_entries "
                        f"WHERE expense_payment_id = {_BILL_PAYMENT})",
                        f"DELETE FROM journal_entries WHERE expense_payment_id = {_BILL_PAYMENT}",
                        f"DELETE FROM expense_payments WHERE expense_payment_id = {_BILL_PAYMENT}"],
    "V-TX-01/expense": [f"UPDATE tax_transactions SET taxable_value = taxable_value + 1 WHERE tax_transaction_id = {_EXPENSE_TAX}"],
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
