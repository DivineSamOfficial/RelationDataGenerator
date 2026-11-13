"""Phase 3 step 3.10: B2B collections by payment profile (bank transfers, cheques that clear or bounce, partial
payments, payments on account), allocation of payments and credits to invoices, and card chargebacks (P09).

Window: August–October 2025. The fixture raises the cheque-bounce, chargeback and advance rates so every path
occurs in a short history. Every receivables and payments rule must pass, and each mutation (rolled back) must
make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

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
RULES = ("V-PY-01",)
WINDOW = {"history.start_date": "2025-08-01", "history.end_date": "2025-10-31",
          "collections.cheque_bounce_share": 0.25, "collections.chargeback_share": 0.05,
          "collections.advance_payer_share": 0.4}


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zc" + uuid.uuid4().hex[:6]
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


def test_b2b_invoices_get_paid(generated):
    _, conn = generated
    settled, total = _rows(conn, """SELECT SUM(a.item_status = 'SETTLED'), COUNT(*) FROM accounts_receivable a
                                    JOIN customers c USING (customer_id) JOIN invoices i ON i.invoice_id = a.invoice_id
                                    WHERE c.customer_type = 'B2B' AND i.due_date < '2025-09-15'""")[0]
    assert settled / total > Decimal("0.7")
    # Direct receipts: bank transfers and cleared cheques post Dr Bank (the collections account) / Cr AR (P08).
    methods = {m for (m,) in _rows(conn, """SELECT DISTINCT m.method_code FROM customer_payments p
                                           JOIN payment_methods m USING (payment_method_id)
                                           WHERE p.payment_type = 'INVOICE_PAYMENT'""")}
    assert {"NEFT", "CHEQUE"} <= methods
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN journal_entries j USING (customer_payment_id)
                         JOIN journal_entry_lines l ON l.journal_entry_id = j.journal_entry_id AND l.debit_amount > 0
                         JOIN bank_accounts b ON b.gl_account_id = l.account_id
                         WHERE p.payment_type IN ('INVOICE_PAYMENT', 'ADVANCE') AND j.journal_source = 'CUSTOMER_PAYMENT'
                           AND b.bank_account_id <> p.bank_account_id""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p
                         LEFT JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id
                         WHERE p.payment_type IN ('INVOICE_PAYMENT', 'ADVANCE') AND p.payment_status = 'SUCCESS'
                           AND j.journal_entry_id IS NULL""") == 0
    # Some customers pay late, some very late; partial payments leave invoices partly settled.
    late, very_late = _rows(conn, """SELECT SUM(x.paid > i.due_date), SUM(x.paid > i.due_date + INTERVAL 20 DAY)
                                     FROM invoices i JOIN (SELECT invoice_id, MAX(allocation_date) AS paid
                                                           FROM customer_payment_allocations GROUP BY 1) x USING (invoice_id)
                                     JOIN customers c USING (customer_id) WHERE c.customer_type = 'B2B'""")[0]
    assert late > 0 and very_late > 0
    assert _one(conn, "SELECT COUNT(*) FROM accounts_receivable WHERE item_status = 'PARTIALLY_SETTLED' "
                      "AND item_type = 'INVOICE'") > 0


def test_cheques_clear_or_bounce(generated):
    _, conn = generated
    statuses = dict(_rows(conn, """SELECT p.payment_status, COUNT(*) FROM customer_payments p
                                   JOIN payment_methods m USING (payment_method_id) WHERE m.method_code = 'CHEQUE'
                                   GROUP BY 1"""))
    assert statuses.get("SUCCESS", 0) > 0 and statuses.get("FAILED", 0) > 0
    # A bounced cheque posts nothing and has no AR item; a cleared one cleared after the clearing lag.
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p
                         LEFT JOIN journal_entries j ON j.customer_payment_id = p.customer_payment_id
                         LEFT JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
                         WHERE p.failure_reason = 'CHEQUE_BOUNCED'
                           AND (j.journal_entry_id IS NOT NULL OR a.accounts_receivable_id IS NOT NULL)""") == 0
    assert _one(conn, """SELECT COUNT(*) FROM customer_payments p JOIN payment_methods m USING (payment_method_id)
                         WHERE m.method_code = 'CHEQUE' AND p.payment_status = 'SUCCESS'
                           AND DATE(p.completed_at) < p.payment_date + INTERVAL m.settlement_lag_days DAY""") == 0
    # Only cheques of the last days are still uncleared.
    assert _one(conn, "SELECT COUNT(*) FROM customer_payments WHERE payment_status = 'INITIATED' "
                      "AND payment_date < '2025-10-25'") == 0


def test_payments_on_account_are_applied(generated):
    _, conn = generated
    advances = _rows(conn, """SELECT a.item_status FROM customer_payments p
                              JOIN accounts_receivable a ON a.customer_payment_id = p.customer_payment_id
                              WHERE p.payment_type = 'ADVANCE'""")
    assert advances and any(status in ("SETTLED", "PARTIALLY_SETTLED") for (status,) in advances)
    # Credits are applied oldest due invoice first: no open credit while the same customer has an invoice open
    # since before the credit (B2B, applied at every payment run).
    assert _one(conn, """SELECT COUNT(*) FROM accounts_receivable c JOIN accounts_receivable i
                         ON i.customer_id = c.customer_id AND i.item_type = 'INVOICE'
                         AND i.item_status IN ('OPEN', 'PARTIALLY_SETTLED') AND i.document_date < c.document_date
                         JOIN customers k ON k.customer_id = c.customer_id
                         WHERE k.customer_type = 'B2B' AND c.item_type IN ('PAYMENT', 'CREDIT_NOTE')
                           AND c.item_status IN ('OPEN', 'PARTIALLY_SETTLED') AND c.document_date < '2025-10-20'""") == 0


def test_chargebacks_reverse_the_receipt(generated):
    _, conn = generated
    reversed_ = _rows(conn, """SELECT p.customer_payment_id, p.reversed_at, p.payment_date FROM customer_payments p
                               WHERE p.payment_status = 'REVERSED'""")
    assert reversed_
    ids = ", ".join(str(r[0]) for r in reversed_)
    assert all(15 <= (r[1].date() - r[2]).days <= 60 for r in reversed_)
    # P09: the receipt's P08 is REVERSED and mirrored; the AR item is REVERSED; allocations are undone.
    assert _one(conn, f"""SELECT COUNT(*) FROM journal_entries j
                          LEFT JOIN journal_entries r ON r.reverses_journal_entry_id = j.journal_entry_id
                          WHERE j.customer_payment_id IN ({ids}) AND (j.entry_status <> 'REVERSED' OR r.journal_entry_id IS NULL)""") == 0
    assert _one(conn, f"""SELECT COUNT(*) FROM accounts_receivable WHERE customer_payment_id IN ({ids})
                          AND item_status <> 'REVERSED'""") == 0
    assert _one(conn, f"""SELECT COUNT(*) FROM customer_payment_allocations WHERE customer_payment_id IN ({ids})
                          AND allocation_status = 'ACTIVE'""") == 0


def test_fewer_credit_holds_expire(generated):
    _, conn = generated
    # With customers paying, credit holds are released by payments instead of expiring.
    released = _one(conn, """SELECT COUNT(*) FROM order_status_history WHERE from_status = 'ON_HOLD'
                             AND to_status = 'CONFIRMED' AND change_reason = 'Credit check passed'""")
    assert released > 0


_ALLOCATION = ("(SELECT x FROM (SELECT MIN(customer_payment_allocation_id) x FROM customer_payment_allocations "
               "WHERE allocation_status = 'ACTIVE' AND invoice_id IS NOT NULL) t)")

MUTATIONS = {
    "V-PY-01": [f"UPDATE customer_payment_allocations SET allocated_amount = allocated_amount + 1 "
                f"WHERE customer_payment_allocation_id = {_ALLOCATION}"],
    "V-PY-01/status": ["UPDATE customer_payments p JOIN customer_payment_allocations x USING (customer_payment_id) "
                       "SET p.payment_status = 'FAILED', p.failure_reason = 'BANK_DECLINED', p.settlement_date = NULL "
                       f"WHERE x.customer_payment_allocation_id = {_ALLOCATION}"],
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
