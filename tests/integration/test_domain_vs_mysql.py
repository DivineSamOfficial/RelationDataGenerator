"""Phase 3 step 3.2: the domain functions produce exactly what the database's own formulas accept.

For thousands of random lines, the values computed by ``scmgen.domain`` are loaded into a temporary
probe table with the real column types, and the **actual CHECK expressions** (read from
information_schema, not copied) are evaluated by MySQL. A tampered row is the negative control.
The moving-average cost is compared with the real generated-column expression of ``inventory``.
"""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl
from scmgen.domain.costing import StockPosition
from scmgen.domain.gst import (
    document_totals,
    price_exclusive,
    price_inclusive,
    split_gst,
    tax_line,
    total_gst,
    transfer_tax,
)

pytestmark = pytest.mark.integration

SCHEMA = "scm_domaintest_oltp"
SLABS = ["0", "0.25", "3", "5", "12", "18", "28", "40"]


@pytest.fixture(scope="module")
def conn():
    engine = create_mysql_engine(load_settings(), role="app")
    with engine.connect() as connection:
        execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(connection, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        try:
            apply_workspace_ddl(connection, SCHEMA)
            execute_raw(connection, f"USE `{SCHEMA}`")
            yield connection
        finally:
            execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    engine.dispose()


def _clause(conn, name: str) -> str:
    clause = conn.execute(text(
        "SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA = :s AND CONSTRAINT_NAME = :n"), {"s": SCHEMA, "n": name}).scalar_one()
    return clause.replace("\\'", "'")


def _failing(conn, table: str, rows: list[dict], expressions: dict[str, str]) -> dict[str, list[int]]:
    """Load ``rows`` into a probe table with ``table``'s column types; return failing row numbers per expression."""
    columns = list(rows[0])
    execute_raw(conn, "DROP TEMPORARY TABLE IF EXISTS probe")
    execute_raw(conn, f"CREATE TEMPORARY TABLE probe AS SELECT 0 AS case_no, {', '.join(columns)} FROM {table} LIMIT 0")
    conn.execute(text(f"INSERT INTO probe (case_no, {', '.join(columns)}) VALUES (:case_no, "
                      f"{', '.join(':' + c for c in columns)})"),
                 [{"case_no": i, **row} for i, row in enumerate(rows)])
    return {name: list(conn.execute(text(
        f"SELECT case_no FROM probe WHERE NOT COALESCE({expression}, FALSE) ORDER BY case_no")).scalars())
        for name, expression in expressions.items()}


def _random_line(rng: random.Random):
    qty = D(rng.choice([rng.randint(1, 20), rng.randint(1, 500_000) / 1000]))
    discount = D(rng.choice([0, 0, 0, rng.randint(1, 900_000) / 10000]))
    gst = D(rng.choice(SLABS))
    if rng.random() < 0.5:
        incl = D(rng.choice([rng.randint(1, 5000) * 100 - 1, rng.randint(1, 50_000_000)])) / 100
        return price_inclusive(qty, incl, discount, gst)
    return price_exclusive(qty, D(rng.randint(1, 50_000_000)) / 10000, discount, gst)


def test_order_lines_satisfy_the_real_checks(conn):
    rng = random.Random(20261112)
    rows = []
    for _ in range(4000):
        line = _random_line(rng)
        tax = total_gst(line.taxable_amount, line.gst_rate)
        rows.append({"quantity_ordered": line.quantity, "unit_price": line.unit_price,
                     "inclusive_unit_price": line.inclusive_unit_price, "discount_percent": line.discount_percent,
                     "taxable_amount": line.taxable_amount, "gst_rate": line.gst_rate, "tax_amount": tax,
                     "line_total": line.taxable_amount + tax})
    rows.append(dict(rows[0], taxable_amount=rows[0]["taxable_amount"] + D("0.01")))   # negative control
    failing = _failing(conn, "order_line_items", rows, {
        "ck_order_line_items_taxable": _clause(conn, "ck_order_line_items_taxable"),
        "ck_order_line_items_tax": _clause(conn, "ck_order_line_items_tax")})
    assert failing["ck_order_line_items_taxable"] == [4000]                 # only the tampered row
    assert failing["ck_order_line_items_tax"] == [4000]


def test_invoice_lines_and_headers_satisfy_the_real_checks(conn):
    rng = random.Random(20261113)
    line_rows, header_rows = [], []
    for _ in range(800):
        inter_state = rng.random() < 0.4
        taxed = []
        for _ in range(rng.randint(1, 6)):
            line = _random_line(rng)
            split = split_gst(line.taxable_amount, line.gst_rate, inter_state)
            taxed.append(tax_line(line.taxable_amount, line.gst_rate, inter_state))
            line_rows.append({"quantity": line.quantity, "unit_price": line.unit_price,
                              "inclusive_unit_price": line.inclusive_unit_price,
                              "discount_percent": line.discount_percent, "taxable_amount": line.taxable_amount,
                              "gst_rate": line.gst_rate, "cgst_amount": split.cgst, "sgst_amount": split.sgst,
                              "igst_amount": split.igst, "line_total": line.taxable_amount + split.total})
        totals = document_totals(taxed)
        header_rows.append({"is_inter_state": inter_state, "taxable_amount": totals.taxable_amount,
                            "cgst_amount": totals.cgst_amount, "sgst_amount": totals.sgst_amount,
                            "igst_amount": totals.igst_amount, "round_off_amount": totals.round_off_amount,
                            "total_amount": totals.total_amount})
    count = len(line_rows)
    line_rows.append(dict(line_rows[0], cgst_amount=line_rows[0]["cgst_amount"] + D("0.01"),
                          igst_amount=D(0)))                                   # negative control
    failing = _failing(conn, "invoice_line_items", line_rows, {
        name: _clause(conn, name) for name in ("ck_invoice_line_items_taxable", "ck_invoice_line_items_tax")})
    assert failing["ck_invoice_line_items_taxable"] == []
    assert failing["ck_invoice_line_items_tax"] == [count]
    failing = _failing(conn, "invoices", header_rows, {
        name: _clause(conn, name) for name in ("ck_invoices_amounts", "ck_invoices_tax_mode")})
    assert failing == {"ck_invoices_amounts": [], "ck_invoices_tax_mode": []}


def test_transfer_lines_satisfy_the_real_check(conn):
    rng = random.Random(20261114)
    rows = []
    for _ in range(2000):
        qty, cost, gst = D(rng.randint(1, 500_000)) / 1000, D(rng.randint(1, 50_000_000)) / 10000, D(rng.choice(SLABS))
        tax = transfer_tax(qty, cost, gst)
        rows.append({"quantity_shipped": qty, "unit_cost": cost, "transfer_value": tax.transfer_value,
                     "gst_rate": gst, "igst_amount": tax.igst_amount})
    failing = _failing(conn, "stock_transfer_line_items", rows,
                       {"value": _clause(conn, "ck_stock_transfer_line_items_value")})
    assert failing["value"] == []


def test_moving_average_cost_equals_the_generated_column(conn):
    """D87: the engine's MAC equals MySQL's stored ``moving_average_cost`` for every position."""
    expression = conn.execute(text(
        "SELECT GENERATION_EXPRESSION FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = 'inventory' AND COLUMN_NAME = 'moving_average_cost'"),
        {"s": SCHEMA}).scalar_one().replace("\\'", "'")
    rng = random.Random(20261115)
    positions = [StockPosition(D(rng.randint(1, 10**7)) / 1000, D(rng.randint(1, 10**9)) / 100) for _ in range(6000)]
    walk = StockPosition()
    for _ in range(2000):                                    # plus positions reached by real movements
        if walk.quantity == 0 or rng.random() < 0.5:
            walk, _ = walk.receive(rng.randint(1, 300), D(rng.randint(100, 900_000)) / 100)
        else:
            walk, _ = walk.issue(D(rng.randint(1, int(walk.quantity * 1000))) / 1000)
        if walk.quantity:
            positions.append(walk)
    rows = [{"qty_sellable": p.quantity, "qty_quarantine": D(0), "qty_defective": D(0), "stock_value": p.value}
            for p in positions]
    execute_raw(conn, "DROP TEMPORARY TABLE IF EXISTS probe")
    execute_raw(conn, "CREATE TEMPORARY TABLE probe AS SELECT 0 AS case_no, qty_sellable, qty_quarantine, "
                      "qty_defective, stock_value FROM inventory LIMIT 0")
    conn.execute(text("INSERT INTO probe VALUES (:case_no, :qty_sellable, :qty_quarantine, :qty_defective, :stock_value)"),
                 [{"case_no": i, **row} for i, row in enumerate(rows)])
    mysql_mac = dict(conn.execute(text(f"SELECT case_no, {expression} FROM probe")).all())
    mismatches = [i for i, p in enumerate(positions) if mysql_mac[i] != p.moving_average_cost]
    assert mismatches == []
    # Rounding once (plain Decimal) would have disagreed with MySQL on some of these positions.
    single = [i for i, p in enumerate(positions)
              if (p.value / p.quantity).quantize(D("0.0001"), "ROUND_HALF_UP") != mysql_mac[i]]
    assert single, "expected the double-rounding cases that D87 exists for"
