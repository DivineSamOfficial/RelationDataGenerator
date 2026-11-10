"""Phase 3 step 3.3: the whole seed fits the real schema (every CHECK, UNIQUE and FK accepts it).

Carriers are left out: each needs its SERVICES supplier, which master data (step 3.5) creates.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl
from scmgen.domain.journal import SYSTEM_ROLES
from scmgen.generation.ids import IdAllocator
from scmgen.generation.seed import load_seed
from scmgen.generation.writer import BatchWriter

pytestmark = pytest.mark.integration

SCHEMA = "scm_seedtest_oltp"
NOW = datetime(2024, 4, 1, 0, 0)


@pytest.fixture(scope="module")
def conn():
    settings = load_settings()
    admin = create_mysql_engine(settings, role="app")
    with admin.connect() as connection:
        execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(connection, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        apply_workspace_ddl(connection, SCHEMA)
    workspace = create_mysql_engine(settings, role="app", database=SCHEMA)
    try:
        with workspace.connect() as connection:
            yield connection
    finally:
        workspace.dispose()
        with admin.connect() as connection:
            execute_raw(connection, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        admin.dispose()


def _stamped(row: dict, **extra) -> dict:
    return {**row, **extra, "created_at": NOW, "updated_at": NOW}


def test_entire_seed_is_accepted_by_the_schema(conn):
    seed = load_seed()
    ids = IdAllocator.from_database(conn, SCHEMA)
    writer = BatchWriter.for_schema(conn, SCHEMA)
    simple = {"states": seed.states, "departments": seed.departments,
              "product_units_of_measure": seed.units_of_measure, "payment_methods": seed.payment_methods,
              "payment_terms": seed.payment_terms, "sales_channels": seed.sales_channels,
              "marketing_channels": seed.marketing_channels}
    pk = {"states": "state_id", "departments": "department_id", "product_units_of_measure": "unit_of_measure_id",
          "payment_methods": "payment_method_id", "payment_terms": "payment_term_id",
          "sales_channels": "sales_channel_id", "marketing_channels": "marketing_channel_id"}
    for table, rows in simple.items():
        for row in rows:                                   # the loader already made columns uniform
            writer.insert(table, _stamped(row, **{pk[table]: ids.next(table)}))

    type_ids = {}
    for row in seed.account_types:
        type_ids[row["account_type_code"]] = ids.next("account_types")
        writer.insert("account_types", _stamped(row, account_type_id=type_ids[row["account_type_code"]]))

    account_ids = {}
    for row in seed.accounts:                              # parents first (self-reference)
        account_ids[row["account_code"]] = ids.next("chart_of_accounts")
        writer.insert("chart_of_accounts", _stamped({
            "account_id": account_ids[row["account_code"]], "account_code": row["account_code"],
            "account_name": row["account_name"], "account_type_id": type_ids[row["account_type_code"]],
            "parent_account_id": account_ids.get(row["parent_account_code"]),
            "account_level": row["account_level"], "is_postable": row["is_postable"],
            "normal_balance": row["normal_balance"], "system_role": row["system_role"],
            "is_control_account": row["is_control_account"], "control_subledger": row["control_subledger"],
            "requires_cost_center": row["requires_cost_center"], "is_derived": False,
            "cash_flow_category": row["cash_flow_category"]}))

    for row in seed.expense_categories:
        category = {k: v for k, v in row.items() if k != "account"}
        writer.insert("expense_categories", _stamped(category, expense_category_id=ids.next("expense_categories"),
                                                     gl_account_id=account_ids[row["account"]]))
    inserted = writer.flush(conn)
    assert inserted["states"] == 37 and inserted["chart_of_accounts"] == len(seed.accounts)
    assert inserted["expense_categories"] == len(seed.expense_categories)

    roles = set(conn.execute(text("SELECT system_role FROM chart_of_accounts WHERE system_role IS NOT NULL")).scalars())
    assert roles == SYSTEM_ROLES                           # V-AC-04 on real rows
    # The composite self-FK makes MySQL itself confirm that every child has its parent's type.
    mismatched = conn.execute(text(
        "SELECT COUNT(*) FROM chart_of_accounts c JOIN chart_of_accounts p ON p.account_id = c.parent_account_id "
        "WHERE LEFT(c.account_code, 1) <> LEFT(p.account_code, 1)")).scalar_one()
    assert mismatched == 0
    conn.rollback()
