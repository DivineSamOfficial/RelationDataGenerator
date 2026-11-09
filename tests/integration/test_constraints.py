"""Phase 2 step 2.6: the constraints actually REJECT bad data (and accept good data).

1. The baseline dataset (baseline_dataset.py) puts a valid row in all 92 tables: every CHECK, FK and
   UNIQUE in the schema must accept it.
2. Schema-wide sweeps, driven by information_schema: every foreign key rejects a dangling reference,
   every NOT NULL column rejects NULL, every enumeration rejects lower-case and unknown values.
3. Business-rule cases: realistic mistakes, each asserting the specific named constraint fired; plus a
   few must-accept cases so the rules aren't too strict.

Each mutation runs in a transaction that is rolled back, so the baseline is never changed.
Runs as scm_app in the throwaway schema scm_ctest_oltp.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pymysql
import pytest

from baseline_dataset import build_baseline
from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl

pytestmark = pytest.mark.integration

SCHEMA = "scm_ctest_oltp"
FK_MISSING_PARENT = 1452
NOT_NULL = 1048
CHECK_VIOLATED = 3819
DUPLICATE = 1062
BOGUS_ID = {"tinyint": 250, "smallint": 65000, "int": 999_999_999, "bigint": 999_999_999}


@dataclass
class Db:
    """Raw DBAPI access to the constraint-test schema plus metadata from information_schema."""

    raw: Any
    ids: dict[str, int]
    pk: dict[str, str] = field(default_factory=dict)
    generated: dict[str, set[str]] = field(default_factory=dict)
    tables_by_key: dict[str, str] = field(default_factory=dict)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = self.raw.cursor()
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        cursor = self.raw.cursor()
        try:
            cursor.execute(sql, params)
        finally:
            cursor.close()

    def row(self, table: str, pk_value: int) -> dict[str, Any]:
        cursor = self.raw.cursor()
        try:
            cursor.execute(f"SELECT * FROM {table} WHERE {self.pk[table]} = %s", (pk_value,))
            values = cursor.fetchone()
            return dict(zip([d[0] for d in cursor.description], values))
        finally:
            cursor.close()

    def update(self, table: str, pk_value: int, changes: dict[str, Any]) -> None:
        assignments = ", ".join(f"{column} = %s" for column in changes)
        self.execute(f"UPDATE {table} SET {assignments} WHERE {self.pk[table]} = %s", [*changes.values(), pk_value])

    def clone(self, table: str, pk_value: int, overrides: dict[str, Any]) -> None:
        """Insert a copy of a baseline row (minus PK and generated columns) with some values changed."""
        values = {
            column: value for column, value in self.row(table, pk_value).items()
            if column != self.pk[table] and column not in self.generated.get(table, set())
        }
        values.update(overrides)
        columns = ", ".join(values)
        self.execute(f"INSERT INTO {table} ({columns}) VALUES ({', '.join(['%s'] * len(values))})", list(values.values()))


@pytest.fixture(scope="module")
def db():
    engine = create_mysql_engine(load_settings(), role="app")
    with engine.connect() as conn:
        execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(conn, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        try:
            apply_workspace_ddl(conn, SCHEMA)
            ids = build_baseline(conn)
            handle = Db(raw=conn.connection, ids=ids)
            for table, column in handle.query(
                "SELECT table_name, column_name FROM information_schema.key_column_usage "
                "WHERE table_schema = %s AND constraint_name = 'PRIMARY'", (SCHEMA,)):
                handle.pk[table] = column
            for table, column in handle.query(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND (extra LIKE '%%STORED GENERATED%%' OR extra LIKE '%%VIRTUAL GENERATED%%')", (SCHEMA,)):
                handle.generated.setdefault(table, set()).add(column)
            yield handle
        finally:
            execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    engine.dispose()


def attempt(db: Db, action: Callable[[], None]) -> pymysql.MySQLError | None:
    """Run `action` in a transaction that is always rolled back; return the MySQL error, if any."""
    db.raw.begin()
    try:
        action()
        return None
    except pymysql.MySQLError as exc:
        return exc
    finally:
        db.raw.rollback()


def base_tables(db: Db) -> list[str]:
    return [r[0] for r in db.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        (SCHEMA,))]


# --- 1. the baseline: every table accepts valid business data ---------------------------------

def test_baseline_fills_all_92_tables(db: Db) -> None:
    tables = base_tables(db)
    assert len(tables) == 92
    empty = [t for t in tables if db.query(f"SELECT COUNT(*) FROM {t}")[0][0] == 0]
    assert empty == [], f"tables without a baseline row: {empty}"


def test_baseline_b2c_line_uses_gst_inclusive_pricing(db: Db) -> None:
    line = db.row("order_line_items", db.ids["order_line_b2c"])
    assert line["inclusive_unit_price"] == 499
    assert line["taxable_amount"] + line["tax_amount"] == 499  # customer pays exactly the shelf price (D79)


# --- 2. schema-wide sweeps --------------------------------------------------------------------

def test_every_foreign_key_rejects_a_dangling_reference(db: Db) -> None:
    rows = db.query(
        "SELECT k.table_name, k.constraint_name, k.column_name, c.data_type "
        "FROM information_schema.key_column_usage k JOIN information_schema.columns c "
        "  ON c.table_schema = k.table_schema AND c.table_name = k.table_name AND c.column_name = k.column_name "
        "WHERE k.table_schema = %s AND k.referenced_table_name IS NOT NULL "
        "ORDER BY k.table_name, k.constraint_name, k.ordinal_position", (SCHEMA,))
    constraints: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for table, name, column, data_type in rows:
        constraints.setdefault((table, name), []).append((column, data_type))

    # How each FK refused the dangling value: 1452 = the FK itself; 1451 = the row is also a parent and a
    # child's composite FK protects the value; 3819 = the column is empty in every baseline row and a CHECK
    # forbids filling it on that row. What must NEVER happen is acceptance (the MySQL defect behind D85).
    outcomes: dict[int, list[str]] = {}
    accepted = []
    for (table, name), columns in constraints.items():
        not_null = " AND ".join(f"{c} IS NOT NULL" for c, _ in columns)
        target = db.query(f"SELECT {db.pk[table]} FROM {table} WHERE {not_null} LIMIT 1") or \
            db.query(f"SELECT {db.pk[table]} FROM {table} LIMIT 1")
        changes = {column: BOGUS_ID[data_type] for column, data_type in columns}
        error = attempt(db, lambda: db.update(table, target[0][0], changes))
        if error is None:
            accepted.append(name)
        else:
            outcomes.setdefault(error.args[0], []).append(name)
    assert len(constraints) >= 300
    assert accepted == [], f"foreign keys that ACCEPTED a dangling reference: {accepted}"
    assert set(outcomes) <= {FK_MISSING_PARENT, 1451, CHECK_VIOLATED}, {k: v[:3] for k, v in outcomes.items()}
    assert len(outcomes.get(FK_MISSING_PARENT, [])) >= 190  # the large majority are refused by the FK itself


def test_every_not_null_column_rejects_null(db: Db) -> None:
    columns = db.query(
        "SELECT table_name, column_name FROM information_schema.columns c WHERE table_schema = %s "
        "AND is_nullable = 'NO' AND column_key <> 'PRI' AND extra NOT LIKE '%%GENERATED%%' "
        "AND EXISTS (SELECT 1 FROM information_schema.tables t WHERE t.table_schema = c.table_schema "
        "            AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE')", (SCHEMA,))
    failures = []
    for table, column in columns:
        first = db.query(f"SELECT {db.pk[table]} FROM {table} LIMIT 1")[0][0]
        error = attempt(db, lambda: db.update(table, first, {column: None}))
        if error is None or error.args[0] != NOT_NULL:
            failures.append((table, column, None if error is None else error.args))
    assert {table for table, _ in columns} == set(base_tables(db))  # every table's NOT NULL columns covered
    assert failures == []


ENUM_CHECK = re.compile(r"^regexp_like\(`(\w+)`,_utf8mb4\\?'\^\(([A-Z0-9_|.\[\]\\]+)\)\$\\?',_utf8mb4\\?'c\\?'\)$")


def test_every_enumeration_rejects_lower_case_and_unknown_values(db: Db) -> None:
    checks = db.query(
        "SELECT tc.table_name, cc.constraint_name, cc.check_clause FROM information_schema.check_constraints cc "
        "JOIN information_schema.table_constraints tc ON tc.constraint_schema = cc.constraint_schema "
        "  AND tc.constraint_name = cc.constraint_name WHERE cc.constraint_schema = %s", (SCHEMA,))
    enums = []
    for table, name, clause in checks:
        match = ENUM_CHECK.match(clause.strip())
        if match and "|" in match.group(2):
            enums.append((table, name, match.group(1)))
    failures = []
    for table, name, column in enums:
        found = db.query(f"SELECT {db.pk[table]}, {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 1")
        if not found:
            continue
        pk_value, value = found[0]
        # 'ZZ' is short enough for every enum column, so the CHECK (not the length limit) must reject it
        for bad in {value.lower(), "ZZ"} - {value}:
            error = attempt(db, lambda: db.update(table, pk_value, {column: bad}))
            if error is None or error.args[0] != CHECK_VIOLATED:
                failures.append((name, bad, None if error is None else error.args))
    assert len(enums) > 100
    assert failures == []


# --- 3. business-rule cases --------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    name: str
    table: str
    key: str                                   # baseline row (BaselineBuilder id key)
    changes: Callable[[dict[str, int]], dict[str, Any]]
    constraint: str | None                     # expected constraint name; None = must be ACCEPTED
    kind: str = "update"                       # "update" the row, or insert a "clone" of it
    code: int = CHECK_VIOLATED


def c(name, table, key, changes, constraint, kind="update", code=None) -> Case:
    if code is None:
        code = {"fk": FK_MISSING_PARENT, "uq": DUPLICATE}.get(constraint.split("_")[0], CHECK_VIOLATED) if constraint else 0
    return Case(name, table, key, changes, constraint, kind, code)


NEW_INVOICE = {"invoice_number": "KA2627-I000099", "shipment_id": None, "irn": None, "irn_generated_at": None}

CASES = [
    # inventory: never negative, reservations within sellable stock, value only with quantity
    c("negative stock", "inventory", "inventory_dc", lambda i: {"qty_sellable": -1}, "ck_inventory_quantities"),
    c("reserve more than sellable", "inventory", "inventory_dc", lambda i: {"qty_reserved": 92}, "ck_inventory_quantities"),
    c("value with zero quantity", "inventory", "inventory_store", lambda i: {"qty_sellable": 0}, "ck_inventory_value"),
    # order-line counters
    c("return more than delivered", "order_line_items", "order_line", lambda i: {"quantity_return_requested": 11},
      "ck_order_line_items_counters"),
    c("invoice more than shipped", "order_line_items", "order_line", lambda i: {"quantity_invoiced": 11},
      "ck_order_line_items_counters"),
    c("B2C inclusive taxable a paisa off", "order_line_items", "order_line_b2c",
      lambda i: {"taxable_amount": "422.89", "line_total": "499.01"}, "ck_order_line_items_taxable"),
    # tax arithmetic (D84)
    c("quotation taxable a paisa off", "quotation_line_items", "quotation_line",
      lambda i: {"taxable_amount": "50000.01", "line_total": "59000.01"}, "ck_quotation_line_items_amounts"),
    c("PO line CGST a paisa off", "purchase_order_line_items", "po_line",
      lambda i: {"cgst_amount": "4500.01", "sgst_amount": "4500.01", "line_total": "59000.02"},
      "ck_purchase_order_line_items_tax"),
    c("invoice line with CGST and IGST", "invoice_line_items", "invoice_line",
      lambda i: {"igst_amount": 1800, "line_total": 13600}, "ck_invoice_line_items_tax"),
    c("invoice total not in whole rupees", "invoices", "invoice",
      lambda i: {"round_off_amount": "0.40", "total_amount": "11800.40"}, "ck_invoices_amounts"),
    c("IGST on an intra-state invoice", "invoices", "invoice",
      lambda i: {"cgst_amount": 0, "sgst_amount": 0, "igst_amount": 1800}, "ck_invoices_tax_mode"),
    c("transfer IGST missing", "stock_transfer_line_items", "transfer_line", lambda i: {"gst_rate": 18},
      "ck_stock_transfer_line_items_value"),
    # invoices tied to order, customer, warehouse and GSTIN (D80, 1D §1.4)
    c("invoice under another warehouse's GSTIN", "invoices", "invoice",
      lambda i: {**NEW_INVOICE, "company_gst_registration_id": i["reg_mh"]}, "fk_invoices_warehouse_registration", "clone"),
    c("invoice for a customer other than the order's", "invoices", "invoice",
      lambda i: {**NEW_INVOICE, "customer_id": i["customer_b2c"], "customer_gstin": None,
                 "billing_address_id": i["addr_b2c"], "shipping_address_id": i["addr_b2c"]},
      "fk_invoices_order_customer", "clone"),
    c("invoice cancelled after 24 hours", "invoices", "invoice",
      lambda i: {"invoice_status": "CANCELLED", "cancelled_at": "2026-04-14 13:00:00", "cancellation_reason": "late"},
      "ck_invoices_cancelled"),
    c("invoice number in the long format", "invoices", "invoice", lambda i: {"invoice_number": "KA2627-X000001"},
      "ck_invoices_number"),
    c("credit note for another customer", "credit_notes", "credit_note",
      lambda i: {"credit_note_number": "KA2627-C000099", "customer_id": i["customer_b2c"]}, "fk_credit_notes_invoice", "clone"),
    # journals (D50, D62)
    c("journal posted twice", "journal_entries", "journal", lambda i: {"journal_number": "JV/2026-27/0000002"},
      "uq_journal_entries_posting_key", "clone"),
    c("unbalanced journal", "journal_entries", "journal", lambda i: {"total_credit": 11799}, "ck_journal_entries_balanced"),
    c("sales-invoice journal without its invoice", "journal_entries", "journal",
      lambda i: {"journal_number": "JV/2026-27/0000003", "invoice_id": None}, "ck_journal_entries_source_document", "clone"),
    c("journal line both debit and credit", "journal_entry_lines", "journal_dr", lambda i: {"credit_amount": 1},
      "ck_journal_entry_lines_one_side"),
    # the inventory ledger (D28, D33)
    c("ledger row with two sources", "inventory_transactions", "mv_receipt",
      lambda i: {"shipment_line_item_id": i["shipment_line"]}, "ck_inventory_transactions_one_source"),
    c("sales issue with positive quantity", "inventory_transactions", "mv_issue",
      lambda i: {"quantity": 10, "value_change": 5000}, "ck_inventory_transactions_type_sign"),
    c("purchase receipt into quarantine", "inventory_transactions", "mv_receipt", lambda i: {"stock_status": "QUARANTINE"},
      "ck_inventory_transactions_type_source"),
    c("value moving against quantity", "inventory_transactions", "mv_receipt", lambda i: {"value_change": -50000},
      "ck_inventory_transactions_values"),
    c("reversal row with opposite sign is accepted", "inventory_transactions", "mv_issue",
      lambda i: {"quantity": 10, "value_change": 5000, "reversal_of_inventory_transaction_id": i["mv_issue"]}, None, "clone"),
    c("batch with two sources", "inventory_batches", "batch", lambda i: {"goods_receipt_line_item_id": i["grn_line"]},
      "ck_inventory_batches_one_source"),
    # master data
    c("second default billing address", "customer_addresses", "addr", lambda i: {"address_label": "Godown"},
      "uq_customer_addresses_default_billing", "clone"),
    c("order using another customer's address", "orders", "order", lambda i: {"billing_address_id": i["addr_b2c"]},
      "fk_orders_billing_address"),
    c("second open-ended GST rate", "product_tax_rates", "tax_rate", lambda i: {"effective_from": "2026-01-01"},
      "uq_product_tax_rates_current", "clone"),
    c("second preferred supplier", "supplier_products", "supplier_product", lambda i: {"supplier_id": i["supplier_svc"]},
      "uq_supplier_products_preferred", "clone"),
    c("warehouse under another state's GSTIN", "warehouses", "wh_store", lambda i: {"state_id": i["state_mh"]},
      "fk_warehouses_registration_state"),
    c("lower-case GSTIN", "customers", "customer", lambda i: {"gstin": "29zzxpc5678d1z9"}, "ck_customers_gstin"),
    c("GSTIN not matching PAN", "customers", "customer", lambda i: {"pan": "ZZXPC9999D"}, "ck_customers_gstin_pan"),
    c("B2C consumer with a GSTIN", "customers", "customer_b2c", lambda i: {"gstin": "29ZZXPC1111D1Z9"},
      "ck_customers_gstin_required"),
    c("account under a parent of another type", "chart_of_accounts", "acc_ar",
      lambda i: {"account_code": "11500", "account_name": "Mismatch", "account_type_id": i["type_liability"],
                 "system_role": None, "is_control_account": False, "control_subledger": None},
      "fk_chart_of_accounts_parent", "clone"),
    c("product in another business unit than its category", "products", "product",
      lambda i: {"business_unit_id": i["bu_home"]}, "fk_products_category_bu"),
    c("sub-category in another business unit", "product_categories", "cat_earphones",
      lambda i: {"category_code": "ELEC-AUD-XYZ", "category_name": "Other", "business_unit_id": i["bu_home"]},
      "fk_product_categories_parent", "clone"),
    c("prepaid term with credit days", "payment_terms", "term_net30", lambda i: {"is_prepaid": True},
      "ck_payment_terms_prepaid"),
    c("closed period without a closer", "accounting_periods", "period",
      lambda i: {"period_status": "CLOSED", "closed_at": "2026-05-07 10:00:00"}, "ck_accounting_periods_closed"),
    c("cost centre linked to the wrong kind of object", "cost_centers", "cc_elec", lambda i: {"warehouse_id": i["wh_dc"]},
      "ck_cost_centers_type_link"),
    c("termination date while still active", "employees", "emp", lambda i: {"termination_date": "2026-12-31"},
      "ck_employees_termination"),
    c("updated before created", "products", "product", lambda i: {"updated_at": "2000-01-01 00:00:00"},
      "ck_products_audit"),
    # procurement
    c("accept and cancel more than ordered", "purchase_order_line_items", "po_line", lambda i: {"quantity_cancelled": 1},
      "ck_purchase_order_line_items_counters"),
    c("supplier invoice from another supplier than the PO's", "supplier_invoices", "supplier_invoice",
      lambda i: {"internal_reference": "PINV/KA/2026-27/000099", "supplier_invoice_number": "X-1",
                 "supplier_id": i["supplier_svc"], "supplier_gstin": "29ZZXPQ9999Z1Z1"},
      "fk_supplier_invoices_po_supplier", "clone"),
    c("goods received at another warehouse than ordered", "goods_receipts", "grn",
      lambda i: {"grn_number": "GRN/KA/2026-27/000099", "warehouse_id": i["wh_store"]}, "fk_goods_receipts_po_warehouse", "clone"),
    # sales, shipping, returns
    c("order cancellation reason without cancelling", "orders", "order", lambda i: {"cancellation_reason": "CUSTOMER_REQUEST"},
      "ck_orders_cancelled"),
    c("delivered before dispatch", "shipments", "shipment", lambda i: {"delivered_at": "2026-04-12 00:00:00"},
      "ck_shipments_times"),
    c("second successful delivery", "delivery_attempts", "attempt", lambda i: {"attempt_number": 2},
      "uq_delivery_attempts_delivered", "clone"),
    c("tracking row not recorded on receipt", "shipment_tracking", "tracking",
      lambda i: {"created_at": "2026-04-13 13:00:00", "updated_at": "2026-04-13 13:00:00"},
      "ck_shipment_tracking_recorded_on_receipt"),
    c("transfer to the same warehouse", "stock_transfers", "transfer", lambda i: {"destination_warehouse_id": i["wh_dc"]},
      "ck_stock_transfers_distinct"),
    c("receive more returns than approved", "sales_return_line_items", "return_line", lambda i: {"quantity_received": 3},
      "ck_sales_return_line_items_quantities"),
    c("return line for an order line the invoice line didn't bill", "sales_return_line_items", "return_line",
      lambda i: {"order_line_item_id": i["order_line_b2c"]}, "fk_sales_return_line_items_invoice_line"),
    c("defect register row with two causes", "defective_stock", "defect",
      lambda i: {"stock_adjustment_line_item_id": i["adjustment_line"]}, "ck_defective_stock_one_source"),
    # receivables, payables, bank
    c("AR item for another customer than its invoice", "accounts_receivable", "ar_item",
      lambda i: {"customer_id": i["customer_b2c"]}, "fk_accounts_receivable_invoice"),
    c("AR invoice item with negative amount", "accounts_receivable", "ar_item", lambda i: {"original_amount": -11800},
      "ck_accounts_receivable_sign"),
    c("AP item with two payees", "accounts_payable", "ap_item", lambda i: {"employee_id": i["emp"]}, "ck_accounts_payable_payee"),
    c("allocation with two credit sides", "customer_payment_allocations", "allocation",
      lambda i: {"credit_note_id": i["credit_note"]}, "ck_customer_payment_allocations_sides"),
    c("supplier payment shown as money in", "bank_transactions", "bank_txn",
      lambda i: {"transaction_type": "SUPPLIER_PAYMENT", "customer_payment_id": None, "supplier_payment_id": i["supplier_payment"]},
      "ck_bank_transactions_direction"),
    c("reconciled with a difference", "bank_reconciliations", "reconciliation",
      lambda i: {"book_closing_balance": 100001, "difference_amount": 1}, "ck_bank_reconciliations_reconciled"),
    # GST (D58)
    c("filed GSTR-3B not settled", "gst_returns", "gst_return", lambda i: {"itc_used_cgst": 0, "itc_used_sgst": 0},
      "ck_gst_returns_settlement"),
    c("CGST credit paying SGST", "gst_return_line_items", "gst_line_paid", lambda i: {"sgst_amount": 900},
      "ck_gst_return_line_items_cross_use"),
    c("credit-note tax recorded as positive", "tax_transactions", "tax_txn",
      lambda i: {"document_type": "CREDIT_NOTE", "invoice_line_item_id": None, "credit_note_line_item_id": i["credit_note_line"],
                 "document_number": "KA2627-C000001", "gstr1_section": "CDNR"}, "ck_tax_transactions_sign", "clone"),
    c("second tax row for the same invoice line", "tax_transactions", "tax_txn", lambda i: {},
      "uq_tax_transactions_invoice_line", "clone"),
    c("one cancellation mirror per invoice line is accepted", "tax_transactions", "tax_txn",
      lambda i: {"is_cancellation": True, "taxable_value": -10000, "cgst_amount": -900, "sgst_amount": -900}, None, "clone"),
    # expenses and system
    c("payroll with a supplier payee", "expenses", "expense", lambda i: {"expense_type": "PAYROLL", "vendor_bill_number": None},
      "ck_expenses_payee"),
    c("attachment stored on a real domain", "expense_attachments_metadata", "attachment",
      lambda i: {"storage_uri": "s3://kaveri-docs.com/rent.pdf"}, "ck_expense_attachments_metadata_uri"),
    c("duplicate company-wide numbering series", "document_sequences", "seq_jv", lambda i: {},
      "uq_document_sequences_type_series_year", "clone"),
    c("run committed beyond its simulated window", "generation_runs", "run",
      lambda i: {"committed_through": "2026-05-01 00:00:00"}, "ck_generation_runs_window"),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_business_rule(db: Db, case: Case) -> None:
    changes = case.changes(db.ids)
    pk_value = db.ids[case.key]
    action = (lambda: db.update(case.table, pk_value, changes)) if case.kind == "update" \
        else (lambda: db.clone(case.table, pk_value, changes))
    error = attempt(db, action)
    if case.constraint is None:
        assert error is None, f"valid change was rejected: {error}"
        return
    assert error is not None, f"{case.constraint} did not reject the change"
    assert error.args[0] == case.code, error.args
    assert case.constraint in error.args[1], f"expected {case.constraint}, got: {error.args[1]}"


def test_a_ledger_row_can_be_reversed_only_once(db: Db) -> None:
    def reverse_twice() -> None:
        reversal = {"quantity": 10, "value_change": 5000, "reversal_of_inventory_transaction_id": db.ids["mv_issue"]}
        db.clone("inventory_transactions", db.ids["mv_issue"], reversal)
        db.clone("inventory_transactions", db.ids["mv_issue"], reversal)

    error = attempt(db, reverse_twice)
    assert error is not None and error.args[0] == DUPLICATE
    assert "uq_inventory_transactions_reversal_of" in error.args[1]


def test_baseline_is_unchanged_after_all_mutations(db: Db) -> None:
    """Every mutation above was rolled back."""
    assert db.row("inventory", db.ids["inventory_dc"])["qty_sellable"] == 91
    assert db.query("SELECT COUNT(*) FROM journal_entries")[0][0] == 1
    assert db.query("SELECT COUNT(*) FROM tax_transactions")[0][0] == 1
