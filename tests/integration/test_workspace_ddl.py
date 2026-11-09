"""Phase 2 step 2.4: the workspace DDL builds cleanly and follows the design conventions.

Builds the full workspace schema in the throwaway schema scm_ddltest_oltp (as scm_app) and
inspects it through information_schema, so a convention slip in any table is caught.
Behaviour of individual constraints is tested in step 2.6.
"""

from __future__ import annotations

import pymysql
import pytest

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl, workspace_ddl_files

pytestmark = pytest.mark.integration

SCHEMA = "scm_ddltest_oltp"

# Tables expected so far, by design part. Later batches extend this.
PART_1B_TABLES = {
    "states", "companies", "company_gst_registrations", "business_units", "departments", "employees",
    "warehouses", "warehouse_locations", "customers", "customer_addresses", "suppliers",
    "supplier_addresses", "supplier_products", "product_categories", "product_brands",
    "product_units_of_measure", "product_tax_rates", "products", "payment_methods", "payment_terms",
    "shipping_carriers", "expense_categories", "bank_accounts", "sales_channels", "marketing_channels",
    "marketing_campaigns", "account_types", "chart_of_accounts", "accounting_periods", "cost_centers",
    "tax_accounts",
}
PART_1C_PROCUREMENT_TABLES = {
    "rfq_headers", "rfq_line_items", "rfq_suppliers", "rfq_status_history",
    "quotation_headers", "quotation_line_items", "quotation_status_history",
    "purchase_order_headers", "purchase_order_line_items", "purchase_order_status_history",
    "goods_receipts", "goods_receipt_line_items", "goods_inspections",
    "supplier_invoices", "supplier_invoice_line_items", "supplier_payments",
}
PART_1C_INVENTORY_TABLES = {
    "inventory", "inventory_batches", "inventory_reservations", "stock_adjustments",
    "stock_adjustment_line_items", "stock_transfers", "stock_transfer_line_items", "inventory_transactions",
}
EXPECTED_TABLES = PART_1B_TABLES | PART_1C_PROCUREMENT_TABLES | PART_1C_INVENTORY_TABLES

# *_id columns whose FK target table arrives in a later batch (added there by ALTER TABLE).
# Each later batch removes its entries; after the last batch this set must be empty.
DEFERRED_FOREIGN_KEYS = {
    ("inventory_reservations", "order_line_item_id"),
    ("inventory_transactions", "shipment_line_item_id"),
    ("inventory_transactions", "invoice_line_item_id"),
    ("inventory_transactions", "sales_return_line_item_id"),
    ("inventory_transactions", "return_inspection_id"),
    ("inventory_transactions", "defective_stock_id"),
    ("inventory_transactions", "generation_run_id"),
}

SOFT_DELETE_TABLES = {
    "employees", "warehouse_locations", "customers", "customer_addresses", "suppliers",
    "supplier_addresses", "supplier_products", "products",
}


def _rows(conn, query: str) -> list[tuple]:
    cursor = conn.connection.cursor()
    try:
        cursor.execute(query)
        return list(cursor.fetchall())
    finally:
        cursor.close()


@pytest.fixture(scope="module")
def conn():
    engine = create_mysql_engine(load_settings(), role="app")
    with engine.connect() as connection:
        execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(connection, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        try:
            apply_workspace_ddl(connection, SCHEMA)
            yield connection
        finally:  # drop even when the DDL itself fails, so no test schema is left behind
            execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    engine.dispose()


def test_ddl_files_are_ordered_and_numbered() -> None:
    files = workspace_ddl_files()
    assert files == sorted(files)
    assert all(path.split("/")[-1][:4].isdigit() for path in files)


def test_exactly_the_expected_tables_exist(conn) -> None:
    tables = {r[0] for r in _rows(conn, f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{SCHEMA}'")}
    assert tables == EXPECTED_TABLES


def test_every_table_is_innodb_utf8mb4_and_commented(conn) -> None:
    bad = _rows(conn, f"""SELECT table_name, engine, table_collation, table_comment FROM information_schema.tables
        WHERE table_schema = '{SCHEMA}'
          AND (engine <> 'InnoDB' OR table_collation <> 'utf8mb4_0900_ai_ci' OR table_comment = '')""")
    assert bad == []


def test_primary_keys_are_single_entity_id_columns(conn) -> None:
    rows = _rows(conn, f"""SELECT table_name, GROUP_CONCAT(column_name) FROM information_schema.key_column_usage
        WHERE table_schema = '{SCHEMA}' AND constraint_name = 'PRIMARY' GROUP BY table_name""")
    assert len(rows) == len(EXPECTED_TABLES)
    for table, columns in rows:
        assert "," not in columns and columns.endswith("_id"), (table, columns)


def test_audit_columns_everywhere_without_on_update(conn) -> None:
    rows = _rows(conn, f"""SELECT table_name, column_name, column_type, is_nullable, extra
        FROM information_schema.columns
        WHERE table_schema = '{SCHEMA}' AND column_name IN ('created_at', 'updated_at', 'deleted_at')""")
    by_table: dict[str, dict[str, tuple]] = {}
    for table, column, ctype, nullable, extra in rows:
        by_table.setdefault(table, {})[column] = (ctype, nullable, extra)
    for table in EXPECTED_TABLES:
        cols = by_table.get(table, {})
        for name in ("created_at", "updated_at"):
            assert cols.get(name, ("", "", ""))[:2] == ("datetime(6)", "NO"), (table, name)
            assert "on update" not in cols[name][2].lower(), (table, name, "D21: no ON UPDATE")
        assert ("deleted_at" in cols) == (table in SOFT_DELETE_TABLES), (table, "deleted_at")


def test_no_floating_point_columns(conn) -> None:
    assert _rows(conn, f"""SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = '{SCHEMA}' AND data_type IN ('float', 'double', 'real')""") == []


def test_foreign_keys_are_restrict_and_named(conn) -> None:
    rows = _rows(conn, f"""SELECT table_name, constraint_name, update_rule, delete_rule
        FROM information_schema.referential_constraints WHERE constraint_schema = '{SCHEMA}'""")
    assert rows, "expected foreign keys"
    for table, name, update_rule, delete_rule in rows:
        assert name.startswith(f"fk_{table}_"), name
        assert {update_rule, delete_rule} <= {"NO ACTION", "RESTRICT"}, (name, update_rule, delete_rule)


def test_every_id_column_has_a_foreign_key_unless_deferred(conn) -> None:
    """Every non-PK *_id column references another table, apart from the explicit deferred list."""
    id_columns = {(t, c) for t, c in _rows(conn, f"""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
        WHERE c.table_schema = '{SCHEMA}' AND c.column_name LIKE '%\\_id'
          AND c.column_key <> 'PRI'""")}
    fk_columns = {(t, c) for t, c in _rows(conn, f"""
        SELECT table_name, column_name FROM information_schema.key_column_usage
        WHERE table_schema = '{SCHEMA}' AND referenced_table_name IS NOT NULL""")}
    assert id_columns - fk_columns == DEFERRED_FOREIGN_KEYS


def test_indexes_follow_naming_convention(conn) -> None:
    rows = _rows(conn, f"""SELECT DISTINCT table_name, index_name, non_unique FROM information_schema.statistics
        WHERE table_schema = '{SCHEMA}' AND index_name <> 'PRIMARY'""")
    fk_names = {r[0] for r in _rows(conn, f"""SELECT constraint_name FROM information_schema.referential_constraints
        WHERE constraint_schema = '{SCHEMA}'""")}
    for table, index, non_unique in rows:
        if non_unique == 0:
            assert index.startswith(f"uq_{table}_"), index
        else:  # explicit secondary index, or the index MySQL creates for an FK (named after it)
            assert index.startswith(f"ix_{table}_") or index in fk_names, index


def test_check_constraints_are_named_and_case_sensitive(conn) -> None:
    rows = _rows(conn, f"""SELECT tc.table_name, cc.constraint_name, cc.check_clause
        FROM information_schema.check_constraints cc
        JOIN information_schema.table_constraints tc
          ON tc.constraint_schema = cc.constraint_schema AND tc.constraint_name = cc.constraint_name
        WHERE cc.constraint_schema = '{SCHEMA}' AND tc.constraint_type = 'CHECK'""")
    assert {row[0] for row in rows} == EXPECTED_TABLES  # every table has CHECKs (at least ck_<t>_audit)
    assert {f"ck_{t}_audit" for t in EXPECTED_TABLES} <= {row[1] for row in rows}
    for table, name, clause in rows:
        assert name.startswith(f"ck_{table}_"), name
        lowered = clause.lower().replace("\\'", "'")  # information_schema stores quotes escaped
        assert " regexp " not in lowered, (name, "use REGEXP_LIKE(..., 'c'), not plain REGEXP")
        # every REGEXP_LIKE carries the case-sensitive 'c' flag
        assert lowered.count("regexp_like(") == lowered.count("'c')"), (name, clause)


def test_enumeration_checks_are_case_sensitive(conn) -> None:
    """D82 in action: a lower-case status value is rejected."""
    execute_raw(conn, f"USE `{SCHEMA}`")
    with pytest.raises(pymysql.MySQLError) as info:
        execute_raw(conn, """INSERT INTO account_types
            (account_type_code, account_type_name, normal_balance, financial_statement, code_prefix, display_order)
            VALUES ('ASSET', 'Assets', 'debit', 'BALANCE_SHEET', '1', 1)""")
    assert info.value.args[0] == 3819  # check constraint violated
    execute_raw(conn, """INSERT INTO account_types
        (account_type_code, account_type_name, normal_balance, financial_statement, code_prefix, display_order)
        VALUES ('ASSET', 'Assets', 'DEBIT', 'BALANCE_SHEET', '1', 1)""")
    execute_raw(conn, "ROLLBACK")
