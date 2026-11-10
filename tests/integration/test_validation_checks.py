"""Phase 3 step 3.4: the V-MD and V-SY checks pass on consistent data and catch each kind of violation.

The step 2.6 baseline satisfies every constraint but was hand-written before these cross-table rules
existed, so the fixture first repairs what the checks rightly report (GSTIN check digits, missing
warehouse zones, a second supplier, a Marketing campaign owner, sequence rows, the run window). Then
every implemented rule must pass, and each mutation (always rolled back) must make its rule fail.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from baseline_dataset import build_baseline
from sqlalchemy import text

from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl
from scmgen.db.workspaces import create_workspace, ensure_validated, record_validation_outcome
from scmgen.domain.identifiers import gstin_check_character
from scmgen.generation.config import load_generation_config
from scmgen.generation.master.organisation import ZONE_PREFIX
from scmgen.generation.runs import RunRecorder
from scmgen.validation import framework
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.checks.system import sequences_match_issued_numbers
from scmgen.validation.framework import ValidationContext, run_checks

pytestmark = pytest.mark.integration

SCHEMA = "scm_valtest_oltp"
CATALOGUE = load_catalogue()
# This file covers the master-data and system rules on the step 2.6 baseline; the accounting and inventory
# rules are tested on generated data (test_validation_generated.py).
# V-MD-06 needs documents (employees on POs, journals ...): its pass and mutation live in test_procurement.
IMPLEMENTED = sorted(r for r in framework.registered_checks() if r.startswith(("V-MD", "V-SY")) and r != "V-MD-06")


def _clone(conn, table: str, pk: str, pk_value: int, **changes) -> int:
    generated = set(conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
        "AND EXTRA LIKE '%GENERATED%' AND EXTRA NOT LIKE '%DEFAULT_GENERATED%'"), {"t": table}).scalars())
    row = conn.execute(text(f"SELECT * FROM {table} WHERE {pk} = :v"), {"v": pk_value}).mappings().one()
    values = {k: v for k, v in row.items() if k not in generated and k != pk} | changes
    result = conn.execute(text(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})"),
                          values)
    return int(result.lastrowid)


def _repair(conn, ids: dict[str, int]) -> None:
    run = conn.execute
    # V-MD-02: the baseline's hand-typed GSTINs get correct check characters, everywhere they appear.
    gstin_columns = conn.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS c WHERE TABLE_SCHEMA = DATABASE() "
        "AND COLUMN_NAME LIKE '%gstin' AND COLUMN_TYPE = 'char(15)' AND EXISTS (SELECT 1 FROM information_schema.TABLES t "
        "WHERE t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME AND t.TABLE_TYPE = 'BASE TABLE')")).all()
    for (old,) in conn.execute(text("SELECT gstin FROM company_gst_registrations UNION SELECT gstin FROM customers "
                                    "WHERE gstin IS NOT NULL UNION SELECT gstin FROM suppliers WHERE gstin IS NOT NULL")):
        new = old[:14] + gstin_check_character(old[:14])
        for table, column in gstin_columns:
            run(text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"), {"new": new, "old": old})
    # V-MD-07: the Maharashtra GSTIN has no warehouse in the baseline story.
    run(text("UPDATE company_gst_registrations SET registration_status = 'SUSPENDED' "
             "WHERE company_gst_registration_id = :i"), {"i": ids["reg_mh"]})
    # V-MD-09: every warehouse gets its receiving, quarantine and defective zones.
    for warehouse in ("wh_dc", "wh_store"):
        for zone in ("RECEIVING", "QUARANTINE", "DEFECTIVE"):
            exists = run(text("SELECT COUNT(*) FROM warehouse_locations WHERE warehouse_id = :w AND zone = :z"),
                         {"w": ids[warehouse], "z": zone}).scalar_one()
            if not exists:
                _clone(conn, "warehouse_locations", "warehouse_location_id", ids["loc_rcv"],
                       warehouse_id=ids[warehouse], zone=zone, location_code=f"{ZONE_PREFIX[zone]}-01")
    # V-MD-01: the generator's code formats (5-digit SKU counter).
    run(text("UPDATE products SET sku = CONCAT(LEFT(sku, LENGTH(sku) - 4), '0', RIGHT(sku, 4)) "
             "WHERE sku REGEXP '-[0-9]{4}$'"))
    # V-MD-11: PIN inside the city's range.
    run(text("UPDATE warehouses SET pincode = '560100' WHERE pincode NOT LIKE '560%' AND city = 'Bengaluru'"))
    # V-MD-15: a second approved goods supplier for the product.
    second = _clone(conn, "suppliers", "supplier_id", ids["supplier"], supplier_code="SUP-00003",
                    supplier_name="Deccan Audio Components", pan="ZZXFD4321K",
                    gstin="29ZZXFD4321K1Z" + gstin_check_character("29ZZXFD4321K1Z"),
                    email="sales@deccanaudio.example")
    _clone(conn, "supplier_products", "supplier_product_id", ids["supplier_product"], supplier_id=second,
           is_preferred=False, supplier_sku="DAC-EAR-01")
    # V-MD-22: campaigns are owned by Marketing.
    run(text("INSERT INTO departments (department_code, department_name) VALUES ('MKT', 'Marketing')"))
    marketing = run(text("SELECT department_id FROM departments WHERE department_code = 'MKT'")).scalar_one()
    owner = _clone(conn, "employees", "employee_id", ids["emp"], employee_code="EMP-00002", first_name="Meera",
                   email="meera.iyer@kaverinexus.example", department_id=marketing, manager_employee_id=ids["emp"])
    run(text("UPDATE marketing_campaigns SET owner_employee_id = :o"), {"o": owner})
    # V-SY-03: the baseline rows carry wall-clock audit times; let the run's committed window cover them.
    run(text("UPDATE generation_runs SET sim_end_date = '2099-12-31', committed_through = '2099-12-31 23:59:59.999999'"))
    # V-SY-01: one sequence row per issued series, at its highest number.
    ctx = ValidationContext(conn)
    states = dict(run(text("SELECT s.state_code, r.company_gst_registration_id FROM company_gst_registrations r "
                           "JOIN states s ON s.state_id = r.state_id")).all())
    for problem in sequences_match_issued_numbers(ctx):
        run(text("INSERT INTO document_sequences (document_type, company_gst_registration_id, fiscal_year, last_sequence) "
                 "VALUES (:t, :r, :y, :n) AS new ON DUPLICATE KEY UPDATE last_sequence = new.last_sequence"),
            {"t": problem["document_type"], "r": states.get(problem["state_code"]), "y": problem["fiscal_year"],
             "n": problem["highest_issued"]})
    conn.commit()


@pytest.fixture(scope="module")
def db():
    settings = load_settings()
    admin = create_mysql_engine(settings, role="app")
    with admin.connect() as conn:
        execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(conn, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        apply_workspace_ddl(conn, SCHEMA)
    engine = create_mysql_engine(settings, role="app", database=SCHEMA)
    try:
        with engine.connect() as conn:
            ids = build_baseline(conn)
            _repair(conn, ids)
            yield conn, ids
    finally:
        engine.dispose()
        with admin.connect() as conn:
            execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        admin.dispose()


def _run(conn, rule_ids=None, modules=None):
    return run_checks(ValidationContext(conn, workspace_code="kaveri"), CATALOGUE, rule_ids=rule_ids, modules=modules)


def test_consistent_master_data_passes_every_implemented_rule(db):
    conn, _ = db
    report = _run(conn, modules=["MD", "SY"])
    results = {r.rule_id: r for r in report.results}
    failing = {r.rule_id: (r.status, r.message, r.sample[:2]) for r in report.results if r.status in ("FAILED", "ERROR")}
    assert failing == {}
    assert {rid for rid, r in results.items() if r.status == "PASSED"} == set(IMPLEMENTED) | {"V-MD-06"}
    assert {rid for rid, r in results.items() if r.status == "PENDING"} == {"V-SY-04"}
    assert report.passed


MUTATIONS = {
    "V-MD-01": ["UPDATE customers SET customer_code = 'CUST-1234567' WHERE customer_id = (SELECT MIN(customer_id) FROM "
                "(SELECT customer_id FROM customers) c)"],
    "V-MD-02": ["UPDATE customers SET gstin = CONCAT(LEFT(gstin, 14), IF(RIGHT(gstin, 1) = 'A', 'B', 'A')) "
                "WHERE gstin IS NOT NULL"],
    "V-MD-03": ["UPDATE customer_addresses SET state_id = (SELECT state_id FROM states WHERE state_code = 'MH') "
                "WHERE is_default_billing AND customer_id = (SELECT MIN(customer_id) FROM customers WHERE gstin IS NOT NULL)"],
    "V-MD-04": ["UPDATE companies SET company_code = 'otherco'"],
    "V-MD-05": ["UPDATE employees SET manager_employee_id = (SELECT id FROM (SELECT MAX(employee_id) AS id FROM employees) x) "
                "WHERE manager_employee_id IS NULL"],
    "V-MD-07": ["UPDATE company_gst_registrations SET registration_status = 'ACTIVE'"],
    "V-MD-08": ["UPDATE warehouses SET warehouse_status = 'INACTIVE' WHERE warehouse_type = 'DISTRIBUTION_CENTRE'"],
    "V-MD-09": ["UPDATE warehouse_locations SET is_active = FALSE WHERE zone = 'QUARANTINE'"],
    "V-MD-10": ["UPDATE customer_addresses SET is_default_shipping = FALSE"],
    "V-MD-11": ["UPDATE customer_addresses SET pincode = '400001' WHERE city = 'Bengaluru'"],
    "V-MD-12": ["UPDATE customers SET acquisition_marketing_campaign_id = (SELECT MIN(marketing_campaign_id) FROM marketing_campaigns), "
                "acquisition_marketing_channel_id = (SELECT MIN(marketing_channel_id) FROM marketing_campaigns), "
                "acquisition_date = '2026-01-01'"],
    "V-MD-13": ["INSERT INTO customer_addresses (customer_id, address_label, is_billing_address, is_shipping_address, "
                "address_line1, city, state_id, pincode, previous_customer_address_id, valid_from) "
                "SELECT customer_id, 'Moved office', TRUE, TRUE, address_line1, city, state_id, pincode, customer_address_id, "
                "valid_from + INTERVAL 30 DAY "
                "FROM customer_addresses WHERE customer_id = (SELECT MIN(customer_id) FROM customers) LIMIT 1"],
    "V-MD-14": ["UPDATE suppliers SET msme_category = 'MICRO'", "UPDATE payment_terms SET due_days = 60"],
    "V-MD-15": ["UPDATE supplier_products SET supplier_product_status = 'SUSPENDED' WHERE NOT is_preferred"],
    "V-MD-16": ["UPDATE product_categories SET is_leaf = FALSE WHERE category_code = 'ELEC-AUD-EAR'"],
    "V-MD-17": ["UPDATE products SET style_code = 'STYLE-1'",
                "INSERT INTO products (sku, product_name, business_unit_id, product_category_id, product_brand_id, "
                "base_unit_of_measure_id, hsn_code, style_code, list_price, weight_kg, volume_litres, reorder_level, "
                "reorder_quantity, launch_date) SELECT 'SKU-VARIANT-2', product_name, business_unit_id, product_category_id, "
                "product_brand_id, base_unit_of_measure_id, '85182200', style_code, list_price, weight_kg, volume_litres, "
                "reorder_level, reorder_quantity, launch_date FROM products LIMIT 1"],
    "V-MD-18": ["UPDATE products SET mrp = list_price"],
    "V-MD-19": ["INSERT INTO products (sku, product_name, business_unit_id, product_category_id, product_brand_id, "
                "base_unit_of_measure_id, hsn_code, list_price, weight_kg, volume_litres, reorder_level, reorder_quantity, "
                "launch_date) SELECT 'SKU-SAME-HSN', product_name, business_unit_id, product_category_id, product_brand_id, "
                "base_unit_of_measure_id, hsn_code, list_price, weight_kg, volume_litres, reorder_level, reorder_quantity, "
                "launch_date FROM products LIMIT 1",
                "INSERT INTO product_tax_rates (product_id, gst_rate, effective_from, change_reason) "
                "SELECT product_id, 12, launch_date, 'INITIAL' FROM products WHERE sku = 'SKU-SAME-HSN'"],
    "V-MD-20": ["UPDATE product_tax_rates SET gst_rate = 12"],
    "V-MD-21": ["UPDATE product_tax_rates SET effective_from = effective_from + INTERVAL 1 DAY"],
    "V-MD-22": ["UPDATE marketing_channels SET supports_campaigns = FALSE, channel_group = 'REFERRAL', is_paid = FALSE "
                "WHERE marketing_channel_id IN (SELECT marketing_channel_id FROM marketing_campaigns)"],
    "V-SY-01": ["UPDATE document_sequences SET last_sequence = last_sequence + 1 WHERE document_type = 'TAX_INVOICE'"],
    "V-SY-02": ["INSERT INTO generation_runs (run_number, build_id, run_mode, triggered_by, base_seed, config_snapshot, "
                "config_hash, app_version, sim_start_date, sim_end_date, run_status) SELECT 2, build_id, 'CONTINUE', 'CLI', "
                "base_seed, config_snapshot, config_hash, app_version, '2026-04-01', '2026-04-30', 'PENDING' "
                "FROM generation_runs WHERE run_number = 1"],
    "V-SY-03": ["UPDATE generation_runs SET committed_through = '2026-04-30 23:59:59.999999'"],
}


def test_every_implemented_rule_has_a_mutation():
    assert set(MUTATIONS) == set(IMPLEMENTED)


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_each_rule_catches_its_violation(db, rule_id):
    conn, _ = db
    try:
        for statement in MUTATIONS[rule_id]:
            conn.execute(text(statement))
        result = _run(conn, rule_ids=[rule_id]).results[0]
        assert result.status == "FAILED", (result.status, result.message)
        assert result.violations >= 1 and result.sample
    finally:
        conn.rollback()


def test_empty_workspace_master_rules_are_not_applicable():
    settings = load_settings()
    schema = "scm_valempty_oltp"
    admin = create_mysql_engine(settings, role="app")
    with admin.connect() as conn:
        execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
        execute_raw(conn, f"CREATE DATABASE `{schema}`")
        try:
            apply_workspace_ddl(conn, schema)
            execute_raw(conn, f"USE `{schema}`")
            report = _run(conn, modules=["MD", "SY"])
            statuses = {r.rule_id: r.status for r in report.results}
            assert all(statuses[r] == "NOT_APPLICABLE" for r in IMPLEMENTED if r.startswith("V-MD"))
            assert all(statuses[r] == "PASSED" for r in IMPLEMENTED if r.startswith("V-SY")) and report.passed
        finally:
            execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
    admin.dispose()


def test_a_broken_check_is_reported_as_error_not_a_crash(db, monkeypatch):
    conn, _ = db
    def broken(ctx):
        return ctx.rows("SELECT no_such_column FROM companies")
    registry = dict(framework._REGISTRY)
    registry["V-MD-06"] = framework.RegisteredCheck("V-MD-06", broken, True)
    monkeypatch.setattr(framework, "_REGISTRY", registry)
    report = _run(conn, rule_ids=["V-MD-06", "V-MD-04"])
    statuses = {r.rule_id: r.status for r in report.results}
    assert statuses == {"V-MD-06": "ERROR", "V-MD-04": "PASSED"} and not report.passed
    assert "no_such_column" in next(r for r in report.results if r.rule_id == "V-MD-06").message


def test_run_records_its_validation_outcome(db):
    conn, _ = db
    config = load_generation_config(preset="small", today=date(2026, 11, 10),
                                    overrides={"history.start_date": "2026-05-01", "history.end_date": "2026-05-31"})
    try:
        conn.execute(text("UPDATE generation_runs SET run_status = 'COMPLETED' WHERE run_status = 'RUNNING'"))
        run = RunRecorder.create(conn, build_id=str(uuid.uuid4()), run_mode="HISTORICAL", triggered_by="CLI",
                                 base_seed=1, config=config, sim_start=config.start_date, sim_end=config.end_date)
        run.record_validation(conn, passed=False, report_path="data/reports/kaveri/run_2_validation.json")
        row = conn.execute(text("SELECT validation_status, validation_report_path FROM generation_runs "
                                "WHERE generation_run_id = :r"), {"r": run.run_id}).one()
        assert row == ("FAILED", "data/reports/kaveri/run_2_validation.json")
    finally:
        conn.rollback()


def test_workspace_gate_flags_and_clears():
    settings = load_settings()
    code = "zv" + uuid.uuid4().hex[:6]
    try:
        create_workspace(settings, code, "Gate Test Traders", ["electronics"])
        assert ensure_validated(settings, code).status == "READY"
        assert record_validation_outcome(settings, code, passed=False) == "NEEDS_ATTENTION"
        with pytest.raises(WorkspaceError, match="NEEDS_ATTENTION"):
            ensure_validated(settings, code)
        assert ensure_validated(settings, code, force=True).status == "NEEDS_ATTENTION"
        assert record_validation_outcome(settings, code, passed=True) == "READY"
    finally:
        engine = create_mysql_engine(settings, role="app")
        with engine.connect() as conn:
            for suffix in ("oltp", "sim"):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `scm_{code}_{suffix}`")
            conn.execute(text("DELETE FROM scm_control.workspaces WHERE workspace_code = :c"), {"c": code})
            conn.commit()
        engine.dispose()
