"""Phase 2 step 2.3: prove the MySQL behaviours the schema design relies on (Part 1H §5).

Each test builds a tiny table shaped like the real design and checks the behaviour on the
installed server. These stay in the suite: a MySQL upgrade that changes any of this fails
loudly here instead of silently corrupting data later.

A1 CHECK enforced                      A5 FK without action clause = RESTRICT, coexists with CHECK
A2 REGEXP in CHECK (case-sensitive)     A6 date arithmetic / boolean sums in CHECK
A3 generated columns: UNIQUE + CHECK    A7 explicit values in AUTO_INCREMENT keys
A4 partly-NULL composite FK not checked E1–E4 strictness facts the generator must respect

Runs as scm_app in the throwaway schema scm_assume_oltp (dropped afterwards).
"""

from __future__ import annotations

from decimal import Decimal

import pymysql
import pytest
from sqlalchemy import Connection

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw

pytestmark = pytest.mark.integration

SCHEMA = "scm_assume_oltp"

FK_CHILD_WITHOUT_PARENT = 1452
FK_PARENT_HAS_CHILDREN = 1451
DUPLICATE_KEY = 1062
CHECK_VIOLATED = 3819
DATA_TOO_LONG = 1406
INVALID_DATETIME = 1292
INVALID_JSON = 3140


@pytest.fixture
def conn():
    """Fresh schema per test, so tests can't influence each other."""
    engine = create_mysql_engine(load_settings(), role="app")
    with engine.connect() as connection:
        execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(
            connection, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        )
        execute_raw(connection, f"USE `{SCHEMA}`")
        yield connection
        execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    engine.dispose()


def run(conn: Connection, *statements: str) -> None:
    for statement in statements:
        execute_raw(conn, statement)


def scalar(conn: Connection, query: str):
    cursor = conn.connection.cursor()
    try:
        cursor.execute(query)
        return cursor.fetchone()[0]
    finally:
        cursor.close()


def expect_error(conn: Connection, statement: str, code: int) -> None:
    with pytest.raises(pymysql.MySQLError) as info:
        execute_raw(conn, statement)
    assert info.value.args[0] == code, info.value


# --- A1: CHECK constraints are enforced ---------------------------------------------------

def test_a1_check_constraints_are_enforced(conn) -> None:
    run(conn, """CREATE TABLE doc_lines (
        id INT PRIMARY KEY,
        qty DECIMAL(18,3) NOT NULL,
        line_status VARCHAR(30) NOT NULL,
        CONSTRAINT ck_doc_lines_qty CHECK (qty > 0),
        CONSTRAINT ck_doc_lines_status CHECK (line_status IN ('OPEN', 'CLOSED')))""")
    run(conn, "INSERT INTO doc_lines VALUES (1, 2.5, 'OPEN')")
    expect_error(conn, "INSERT INTO doc_lines VALUES (2, -1, 'OPEN')", CHECK_VIOLATED)
    expect_error(conn, "INSERT INTO doc_lines VALUES (3, 1, 'BOGUS')", CHECK_VIOLATED)
    expect_error(conn, "UPDATE doc_lines SET qty = 0 WHERE id = 1", CHECK_VIOLATED)  # updates too


# --- A2: REGEXP inside CHECK, and the case-sensitivity pitfall ----------------------------

GSTIN = "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"


def test_a2_plain_regexp_is_case_insensitive_under_ai_ci(conn) -> None:
    # The pitfall: with utf8mb4_0900_ai_ci, [A-Z] also matches lower case.
    assert scalar(conn, "SELECT 'abc' REGEXP '^[A-Z]+$'") == 1
    assert scalar(conn, "SELECT REGEXP_LIKE('abc', '^[A-Z]+$', 'c')") == 0


def test_a2_regexp_like_c_enforces_gstin_format(conn) -> None:
    run(conn, f"""CREATE TABLE parties (
        id INT PRIMARY KEY,
        gstin CHAR(15) NULL,
        pan CHAR(10) NULL,
        CONSTRAINT ck_parties_gstin CHECK (gstin IS NULL OR REGEXP_LIKE(gstin, '{GSTIN}', 'c')),
        CONSTRAINT ck_parties_gstin_pan CHECK (gstin IS NULL OR pan IS NULL OR SUBSTRING(gstin, 3, 10) = pan))""")
    run(conn, "INSERT INTO parties VALUES (1, '29ZZXCK4821M1Z7', 'ZZXCK4821M')")
    run(conn, "INSERT INTO parties VALUES (2, NULL, NULL)")
    expect_error(conn, "INSERT INTO parties VALUES (3, '29zzxck4821m1z7', NULL)", CHECK_VIOLATED)  # lower case
    expect_error(conn, "INSERT INTO parties VALUES (4, '29ZZXCK4821M1X7', NULL)", CHECK_VIOLATED)  # 14th char not Z
    expect_error(conn, "INSERT INTO parties VALUES (5, '29ZZXCK4821M1Z7', 'ZZXCK9999M')", CHECK_VIOLATED)  # PAN mismatch


# --- A3: stored generated columns with UNIQUE indexes and CHECKs --------------------------

def test_a3_generated_unique_key_allows_at_most_one_live_default(conn) -> None:
    run(conn, """CREATE TABLE addresses (
        id INT PRIMARY KEY,
        customer_id INT NOT NULL,
        is_default_billing BOOLEAN NOT NULL,
        deleted_at DATETIME(6) NULL,
        default_billing_key INT GENERATED ALWAYS AS
            (IF(is_default_billing AND deleted_at IS NULL, customer_id, NULL)) STORED,
        UNIQUE KEY uq_addresses_default_billing (default_billing_key))""")
    run(conn,
        "INSERT INTO addresses (id, customer_id, is_default_billing) VALUES (1, 7, TRUE)",
        "INSERT INTO addresses (id, customer_id, is_default_billing) VALUES (2, 7, FALSE)",
        "INSERT INTO addresses (id, customer_id, is_default_billing) VALUES (3, 7, FALSE)")
    expect_error(conn, "INSERT INTO addresses (id, customer_id, is_default_billing) VALUES (4, 7, TRUE)", DUPLICATE_KEY)
    # Versioning: soft-delete the old default, then a new default is allowed.
    run(conn, "UPDATE addresses SET deleted_at = NOW(6) WHERE id = 1",
        "INSERT INTO addresses (id, customer_id, is_default_billing) VALUES (4, 7, TRUE)")
    assert scalar(conn, "SELECT COUNT(*) FROM addresses WHERE default_billing_key = 7") == 1


def test_a3_check_on_generated_column(conn) -> None:
    run(conn, """CREATE TABLE inventory (
        id INT PRIMARY KEY,
        qty_sellable DECIMAL(18,3) NOT NULL,
        qty_reserved DECIMAL(18,3) NOT NULL,
        qty_available DECIMAL(18,3) GENERATED ALWAYS AS (qty_sellable - qty_reserved) STORED,
        CONSTRAINT ck_inventory_available CHECK (qty_available >= 0))""")
    run(conn, "INSERT INTO inventory (id, qty_sellable, qty_reserved) VALUES (1, 10, 4)")
    assert scalar(conn, "SELECT qty_available FROM inventory WHERE id = 1") == Decimal("6.000")
    expect_error(conn, "UPDATE inventory SET qty_reserved = 11 WHERE id = 1", CHECK_VIOLATED)


def test_a3_generated_posting_key_prevents_double_posting(conn) -> None:
    run(conn, """CREATE TABLE journals (
        id INT PRIMARY KEY,
        journal_source VARCHAR(40) NOT NULL,
        invoice_id INT NULL,
        payment_id INT NULL,
        posting_key VARCHAR(80) GENERATED ALWAYS AS
            (CONCAT(journal_source, ':', COALESCE(invoice_id, payment_id))) STORED,
        UNIQUE KEY uq_journals_posting_key (posting_key))""")
    run(conn,
        "INSERT INTO journals (id, journal_source, invoice_id) VALUES (1, 'SALES_INVOICE', 100)",
        "INSERT INTO journals (id, journal_source, payment_id) VALUES (2, 'CUSTOMER_PAYMENT', 100)",
        "INSERT INTO journals (id, journal_source) VALUES (3, 'MANUAL_ADJUSTMENT')",  # NULL key: allowed
        "INSERT INTO journals (id, journal_source) VALUES (4, 'MANUAL_ADJUSTMENT')")  # NULLs never collide
    expect_error(conn, "INSERT INTO journals (id, journal_source, invoice_id) VALUES (5, 'SALES_INVOICE', 100)", DUPLICATE_KEY)


# --- A4: composite FKs with partly NULL columns are not checked ---------------------------

def test_a4_composite_fk_enforces_match_and_skips_when_partly_null(conn) -> None:
    run(conn,
        """CREATE TABLE registrations (
            id INT PRIMARY KEY, state_id INT NOT NULL,
            UNIQUE KEY uq_registrations_id_state (id, state_id))""",
        """CREATE TABLE warehouses (
            id INT PRIMARY KEY, registration_id INT NULL, state_id INT NOT NULL,
            CONSTRAINT fk_warehouses_registration FOREIGN KEY (registration_id, state_id)
                REFERENCES registrations (id, state_id))""",
        "INSERT INTO registrations VALUES (1, 29)")
    run(conn, "INSERT INTO warehouses VALUES (10, 1, 29)")                      # same state: OK
    expect_error(conn, "INSERT INTO warehouses VALUES (11, 1, 27)", FK_CHILD_WITHOUT_PARENT)  # wrong state
    run(conn, "INSERT INTO warehouses VALUES (12, NULL, 27)")                   # partly NULL: not checked


def test_a4_composite_self_fk_keeps_child_in_parent_business_unit(conn) -> None:
    run(conn, """CREATE TABLE categories (
        id INT PRIMARY KEY, parent_id INT NULL, business_unit_id INT NOT NULL,
        UNIQUE KEY uq_categories_id_bu (id, business_unit_id),
        CONSTRAINT fk_categories_parent FOREIGN KEY (parent_id, business_unit_id)
            REFERENCES categories (id, business_unit_id))""")
    run(conn, "INSERT INTO categories VALUES (1, NULL, 5)", "INSERT INTO categories VALUES (2, 1, 5)")
    expect_error(conn, "INSERT INTO categories VALUES (3, 1, 6)", FK_CHILD_WITHOUT_PARENT)


# --- A5: FKs declared without action clauses behave as RESTRICT ---------------------------

def test_a5_fk_without_clauses_restricts_and_coexists_with_check(conn) -> None:
    run(conn,
        "CREATE TABLE products (id INT PRIMARY KEY)",
        """CREATE TABLE order_lines (
            id INT PRIMARY KEY, product_id INT NOT NULL, replaces_line_id INT NULL,
            CONSTRAINT ck_order_lines_not_self CHECK (replaces_line_id IS NULL OR replaces_line_id <> id),
            CONSTRAINT ck_order_lines_product CHECK (product_id > 0),
            CONSTRAINT fk_order_lines_product FOREIGN KEY (product_id) REFERENCES products (id),
            CONSTRAINT fk_order_lines_replaces FOREIGN KEY (replaces_line_id) REFERENCES order_lines (id))""",
        "INSERT INTO products VALUES (1)",
        "INSERT INTO order_lines VALUES (1, 1, NULL)")
    expect_error(conn, "DELETE FROM products WHERE id = 1", FK_PARENT_HAS_CHILDREN)
    expect_error(conn, "UPDATE products SET id = 2 WHERE id = 1", FK_PARENT_HAS_CHILDREN)
    expect_error(conn, "INSERT INTO order_lines VALUES (2, 99, NULL)", FK_CHILD_WITHOUT_PARENT)
    expect_error(conn, "INSERT INTO order_lines VALUES (3, 1, 3)", CHECK_VIOLATED)
    rule = scalar(conn, f"""SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = '{SCHEMA}' AND CONSTRAINT_NAME = 'fk_order_lines_product'""")
    assert rule in ("NO ACTION", "RESTRICT")


def test_a5_explicit_restrict_with_check_is_also_accepted_on_this_server(conn) -> None:
    # Documents server behaviour behind finding F4: explicit RESTRICT + CHECK on the same
    # column is accepted here. D81 (no clause) stays the convention for portability.
    run(conn,
        "CREATE TABLE p (id INT PRIMARY KEY)",
        """CREATE TABLE c (id INT PRIMARY KEY, p_id INT,
            CONSTRAINT ck_c CHECK (p_id <> id),
            CONSTRAINT fk_c FOREIGN KEY (p_id) REFERENCES p (id) ON DELETE RESTRICT ON UPDATE RESTRICT)""")


# --- A6: date arithmetic and boolean sums in CHECK ----------------------------------------

def test_a6_interval_and_date_function_checks(conn) -> None:
    run(conn, """CREATE TABLE invoices (
        id INT PRIMARY KEY,
        invoice_date DATE NOT NULL,
        issued_at DATETIME(6) NOT NULL,
        cancelled_at DATETIME(6) NULL,
        CONSTRAINT ck_invoices_date CHECK (invoice_date = DATE(issued_at)),
        CONSTRAINT ck_invoices_cancel_window CHECK (cancelled_at IS NULL OR cancelled_at <= issued_at + INTERVAL 24 HOUR))""")
    run(conn, "INSERT INTO invoices VALUES (1, '2026-04-03', '2026-04-03 10:00:00', '2026-04-04 09:59:59')")
    expect_error(conn, "INSERT INTO invoices VALUES (2, '2026-04-03', '2026-04-03 10:00:00', '2026-04-04 10:00:01')", CHECK_VIOLATED)
    expect_error(conn, "INSERT INTO invoices VALUES (3, '2026-04-04', '2026-04-03 10:00:00', NULL)", CHECK_VIOLATED)


def test_a6_exactly_one_source_check(conn) -> None:
    run(conn, """CREATE TABLE movements (
        id INT PRIMARY KEY, grn_line_id INT NULL, shipment_line_id INT NULL, adjustment_line_id INT NULL,
        CONSTRAINT ck_movements_one_source CHECK (
            (grn_line_id IS NOT NULL) + (shipment_line_id IS NOT NULL) + (adjustment_line_id IS NOT NULL) = 1))""")
    run(conn, "INSERT INTO movements VALUES (1, 10, NULL, NULL)")
    expect_error(conn, "INSERT INTO movements VALUES (2, NULL, NULL, NULL)", CHECK_VIOLATED)
    expect_error(conn, "INSERT INTO movements VALUES (3, 10, 20, NULL)", CHECK_VIOLATED)


# --- A7: explicit values in AUTO_INCREMENT keys (D20) ---------------------------------------

def test_a7_explicit_ids_and_counter_continues(conn) -> None:
    run(conn, "CREATE TABLE orders (order_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, note VARCHAR(10))")
    run(conn, "INSERT INTO orders (order_id, note) VALUES (1000, 'engine'), (1001, 'engine'), (5, 'engine')")
    run(conn, "INSERT INTO orders (note) VALUES ('manual')")
    assert scalar(conn, "SELECT order_id FROM orders WHERE note = 'manual'") == 1002
    expect_error(conn, "INSERT INTO orders (order_id, note) VALUES (1000, 'dup')", DUPLICATE_KEY)


# --- E1–E4: strictness facts the generator must respect -----------------------------------

def test_e1_decimal_rounds_silently_so_engine_must_round_itself(conn) -> None:
    # Even in strict mode, extra fractional digits are ROUNDED (half away from zero) with
    # only a Note 1265, not rejected. The engine must therefore quantize every amount itself.
    run(conn, "CREATE TABLE money (amount DECIMAL(18,2))",
        "INSERT INTO money VALUES (1.005), (1.004), (-1.005)")
    cursor = conn.connection.cursor()
    try:
        cursor.execute("SELECT amount FROM money")
        assert [row[0] for row in cursor.fetchall()] == [Decimal("1.01"), Decimal("1.00"), Decimal("-1.01")]
    finally:
        cursor.close()


def test_e2_strict_mode_rejects_overlong_strings_and_bad_dates(conn) -> None:
    run(conn, "CREATE TABLE t (code VARCHAR(5), d DATE)")
    expect_error(conn, "INSERT INTO t VALUES ('TOOLONG', '2026-01-01')", DATA_TOO_LONG)
    expect_error(conn, "INSERT INTO t VALUES ('OK', '2026-02-30')", INVALID_DATETIME)


def test_e3_datetime6_keeps_microseconds(conn) -> None:
    run(conn, "CREATE TABLE t (ts DATETIME(6))", "INSERT INTO t VALUES ('2026-04-03 10:00:00.123456')")
    assert scalar(conn, "SELECT MICROSECOND(ts) FROM t") == 123456


def test_e4_json_columns_reject_invalid_json(conn) -> None:
    run(conn, "CREATE TABLE t (j JSON)", "INSERT INTO t VALUES ('[\"beauty\"]')")
    expect_error(conn, "INSERT INTO t VALUES ('not json')", INVALID_JSON)
