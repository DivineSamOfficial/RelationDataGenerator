"""Phase 3 step 3.6b: accounting and inventory rules on generated data.

A small workspace with one simulated month (opening journal, opening stock, periods) must pass every
implemented rule; each mutation (always rolled back) must make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspaces import create_workspace
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine import generate_history
from scmgen.validation import framework
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

CATALOGUE = load_catalogue()
RULES = sorted(r for r in framework.registered_checks()
               if r.startswith(("V-AC", "V-IN")) and r != "V-IN-03")   # V-IN-03 needs orders: tests/integration/test_sales.py


@pytest.fixture(scope="module")
def generated():
    settings = load_settings()
    code = "zg" + uuid.uuid4().hex[:6]
    create_workspace(settings, code, "Kaveri Nexus Distribution Pvt. Ltd.", ["electronics", "beauty"], base_seed=11)
    try:
        config = load_generation_config(preset="small", today=date(2026, 11, 10), overrides={
            "history.start_date": "2025-08-01", "history.end_date": "2025-08-31"})
        result = generate_history(settings, code, config)
        assert result.report.passed
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


def test_generated_data_passes_every_implemented_rule(generated):
    code, conn = generated
    report = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE)
    statuses = {r.rule_id: r.status for r in report.results}
    assert {rid for rid in framework.registered_checks() if statuses[rid] != "PASSED"} == set()
    # The ★ reconciliation in practice: inventory = ledger = GL Inventory, warehouse by warehouse.
    rows = conn.execute(text("""
        SELECT w.warehouse_id, (SELECT SUM(stock_value) FROM inventory i WHERE i.warehouse_id = w.warehouse_id),
               (SELECT SUM(l.debit_amount - l.credit_amount) FROM journal_entry_lines l
                JOIN chart_of_accounts a USING (account_id) WHERE a.system_role = 'INVENTORY' AND l.warehouse_id = w.warehouse_id)
        FROM warehouses w""")).all()
    assert rows and all(stock == gl for _, stock, gl in rows)
    trial = conn.execute(text("SELECT SUM(debit_amount), SUM(credit_amount) FROM journal_entry_lines")).one()
    assert trial[0] == trial[1] > 0


MUTATIONS = {
    "V-IN-05": ["UPDATE inventory_transactions SET quantity = quantity + 1 WHERE inventory_transaction_id = "
                "(SELECT t FROM (SELECT MIN(inventory_transaction_id) t FROM inventory_transactions) x)"],
    "V-IN-10": ["UPDATE inventory_transactions SET transaction_at = '2025-07-30 10:00:00' WHERE inventory_transaction_id = "
                "(SELECT t FROM (SELECT MIN(inventory_transaction_id) t FROM inventory_transactions) x)"],
    "V-AC-01": ["UPDATE chart_of_accounts SET account_code = '24100' WHERE account_code = '14100'"],
    "V-AC-02": ["UPDATE chart_of_accounts SET is_postable = FALSE WHERE account_code = '14100'"],
    "V-AC-03": ["UPDATE business_units SET cogs_account_id = (SELECT account_id FROM (SELECT account_id FROM chart_of_accounts "
                "WHERE account_code = '44000') x) WHERE business_unit_id = (SELECT b FROM (SELECT MIN(business_unit_id) b FROM business_units) y)"],
    "V-AC-04": ["UPDATE chart_of_accounts SET system_role = NULL WHERE system_role = 'GRNI'"],
    "V-AC-05": ["UPDATE accounting_periods SET period_status = 'OPEN', opened_at = '2025-04-01 00:00:00' WHERE period_code = '2025-04'"],
    "V-AC-06": ["DELETE FROM tax_accounts WHERE tax_component = 'IGST' AND tax_direction = 'INPUT' LIMIT 1"],
    "V-AC-07": ["DELETE FROM cost_centers WHERE cost_center_type = 'BUSINESS_UNIT' AND cost_center_id NOT IN "
                "(SELECT DISTINCT cost_center_id FROM journal_entry_lines WHERE cost_center_id IS NOT NULL) LIMIT 1"],
    "V-AC-10": ["UPDATE journal_entries SET total_debit = total_debit + 1, total_credit = total_credit + 1 "
                "ORDER BY journal_entry_id LIMIT 1"],
    "V-AC-11": ["UPDATE journal_entry_lines SET warehouse_id = NULL WHERE warehouse_id IS NOT NULL "
                "ORDER BY journal_entry_line_id LIMIT 1"],
    "V-AC-14": ["UPDATE journal_entries SET posting_date = '2025-08-20' ORDER BY journal_entry_id LIMIT 1"],
    "V-IN-01": ["UPDATE inventory SET qty_sellable = qty_sellable + 1 ORDER BY inventory_id LIMIT 1"],
    "V-IN-02": ["UPDATE inventory_transactions SET quantity = -quantity, value_change = -value_change "
                "ORDER BY inventory_transaction_id LIMIT 1"],
    "V-IN-04": ["UPDATE inventory_transactions SET inventory_batch_id = NULL WHERE inventory_batch_id IS NOT NULL "
                "ORDER BY inventory_transaction_id LIMIT 1"],
}


def test_every_rule_here_has_a_mutation():
    assert set(MUTATIONS) == set(RULES)


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(generated, rule_id):
    code, conn = generated
    try:
        for statement in MUTATIONS[rule_id]:
            conn.execute(text(statement))
        result = run_checks(ValidationContext(conn, workspace_code=code), CATALOGUE, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
    finally:
        conn.rollback()
