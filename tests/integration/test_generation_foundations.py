"""Phase 3 step 3.1: the generation plumbing against real MySQL.

A scratch workspace schema (scm_gentest_oltp) holds the DDL plus the step 2.6 baseline dataset (which
already has run 1 and two document series), so everything is tested continuing from existing data.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime

import pytest
from baseline_dataset import build_baseline
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl
from scmgen.generation.config import load_generation_config
from scmgen.generation.ids import IdAllocator
from scmgen.generation.numbering import DocumentNumbers, Registration
from scmgen.generation.runs import STEP_NAMES, RunError, RunRecorder
from scmgen.generation.schema_info import LATE_LINK_FOREIGN_KEYS, dependency_order, primary_keys
from scmgen.generation.writer import BatchWriter, WriterError

pytestmark = pytest.mark.integration

SCHEMA = "scm_gentest_oltp"
NOW = datetime(2026, 4, 30, 18, 0)


@pytest.fixture(scope="module")
def engine():
    settings = load_settings()
    admin = create_mysql_engine(settings, role="app")
    with admin.connect() as conn:
        execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        execute_raw(conn, f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    try:
        with admin.connect() as conn:
            apply_workspace_ddl(conn, SCHEMA)
            build_baseline(conn)
        workspace = create_mysql_engine(settings, role="app", database=SCHEMA)
        yield workspace
        workspace.dispose()
    finally:
        with admin.connect() as conn:
            execute_raw(conn, f"DROP DATABASE IF EXISTS `{SCHEMA}`")
        admin.dispose()


@pytest.fixture
def conn(engine):
    with engine.connect() as connection:
        yield connection
        connection.rollback()


def _ka(conn) -> Registration:
    reg_id = conn.execute(text(
        "SELECT r.company_gst_registration_id FROM company_gst_registrations r JOIN states s USING (state_id) "
        "WHERE s.state_code = 'KA'")).scalar_one()
    return Registration(reg_id, "KA")


# --------------------------------------------------------------------------- schema info and IDs


def test_dependency_order_puts_every_fk_target_first(conn):
    order = dependency_order(conn, SCHEMA)
    assert len(order) == 92
    position = {table: i for i, table in enumerate(order)}
    edges = conn.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL AND TABLE_NAME <> REFERENCED_TABLE_NAME"),
        {"s": SCHEMA}).all()
    backwards = {(child, column) for child, column, parent in edges if position[parent] > position[child]}
    assert len(edges) > 300
    # Only the two cross-table late links (D86) point to a table ordered later; the writer sets them by UPDATE.
    # The third late link (bank transfer legs, 3.14a) is a self-reference, which ordering ignores.
    assert backwards == {link for link in LATE_LINK_FOREIGN_KEYS if link[0] != "bank_transactions"}
    assert order[0] == "account_types"                 # level 0 tables first, alphabetical


def test_id_allocator_continues_after_existing_rows(conn):
    keys = primary_keys(conn, SCHEMA)
    assert len(keys) == 92 and keys["states"].max_value == 65535
    ids = IdAllocator.from_database(conn, SCHEMA)
    max_state = conn.execute(text("SELECT MAX(state_id) FROM states")).scalar_one()
    assert ids.next("states") == max_state + 1
    assert ids.next("rfq_line_items") == conn.execute(text("SELECT MAX(rfq_line_item_id) FROM rfq_line_items")).scalar_one() + 1


# --------------------------------------------------------------------------- writer


def _brand(brand_id: int, moment: datetime) -> dict:
    return {"product_brand_id": brand_id, "brand_code": f"GEN{brand_id:05d}", "brand_name": f"Generated Brand {brand_id}",
            "brand_tier": "MID", "created_at": moment, "updated_at": moment}


def test_writer_inserts_parents_first_in_multi_row_batches_then_updates(conn):
    ids = IdAllocator.from_database(conn, SCHEMA)
    writer = BatchWriter.for_schema(conn, SCHEMA, batch_size=500)
    state_id = ids.next("states")
    brand_ids = ids.block("product_brands", 5000)
    started = time.perf_counter()
    for brand_id in brand_ids:
        writer.insert("product_brands", _brand(brand_id, NOW))
    writer.insert("states", {"state_id": state_id, "gst_state_code": "33", "state_code": "TN",
                             "state_name": "Tamil Nadu", "region": "SOUTH", "created_at": NOW, "updated_at": NOW})
    writer.update("product_brands", "product_brand_id", brand_ids[0], {"brand_tier": "PREMIUM", "updated_at": NOW})
    inserted = writer.flush(conn)
    elapsed = time.perf_counter() - started
    assert inserted == {"states": 1, "product_brands": 5000}
    assert conn.execute(text("SELECT brand_tier FROM product_brands WHERE product_brand_id = :i"),
                        {"i": brand_ids[0]}).scalar_one() == "PREMIUM"
    assert elapsed < 5, f"5,000 rows took {elapsed:.1f}s: multi-row INSERTs are not being used"
    assert writer.pending_rows == 0 and writer.written["product_brands"] == 5000


def test_writer_reports_updates_of_missing_rows(conn):
    writer = BatchWriter.for_schema(conn, SCHEMA)
    writer.update("product_brands", "product_brand_id", 999_999_999, {"brand_tier": "MID"})
    with pytest.raises(WriterError, match="not found"):
        writer.flush(conn)


def test_writer_rows_still_meet_the_constraints(conn):
    writer = BatchWriter.for_schema(conn, SCHEMA)
    ids = IdAllocator.from_database(conn, SCHEMA)
    row = _brand(ids.next("product_brands"), NOW) | {"brand_tier": "mid"}       # lower case: CHECK rejects (D82)
    writer.insert("product_brands", row)
    with pytest.raises(Exception, match="3819"):
        writer.flush(conn)


# --------------------------------------------------------------------------- numbering (D75)


def test_numbers_continue_existing_series_and_start_new_ones(conn):
    numbers = DocumentNumbers(fy_start_month=4)
    ka = _ka(conn)
    # The baseline issued tax invoice 1 and journal 1 in FY 2026-27.
    assert numbers.next(conn, "TAX_INVOICE", date(2026, 4, 30), ka) == "KA2627-I000002"
    assert numbers.next(conn, "TAX_INVOICE", date(2026, 4, 30), ka) == "KA2627-I000003"
    assert numbers.next(conn, "JV", date(2026, 4, 30)) == "JV/2026-27/0000002"
    assert numbers.next(conn, "SO", date(2026, 4, 30), ka) == "SO/KA/2026-27/000001"
    assert numbers.next(conn, "SO", date(2027, 4, 1), ka) == "SO/KA/2027-28/000001"     # new FY, new series
    assert numbers.flush(conn) == 4
    rows = dict(conn.execute(text(
        "SELECT CONCAT(document_type, ':', fiscal_year), last_sequence FROM document_sequences")).all())
    assert rows == {"TAX_INVOICE:2026": 3, "JV:2026": 2, "SO:2026": 1, "SO:2027": 1}
    # After a flush the same day continues from the written value.
    assert numbers.next(conn, "TAX_INVOICE", date(2026, 4, 30), ka) == "KA2627-I000004"


def test_a_rolled_back_day_releases_its_numbers(engine):
    """D75: committed tax-invoice numbers stay consecutive although a day failed half way."""
    numbers = DocumentNumbers(fy_start_month=4)
    with engine.connect() as conn:
        ka = _ka(conn)
        first_try = [numbers.next(conn, "TAX_INVOICE", date(2026, 4, 30), ka) for _ in range(3)]
        numbers.flush(conn)
        conn.rollback()                                   # the day failed after writing its numbers
        numbers.discard()
        second_try = [numbers.next(conn, "TAX_INVOICE", date(2026, 4, 30), ka) for _ in range(3)]
        numbers.flush(conn)
        assert second_try == first_try == ["KA2627-I000002", "KA2627-I000003", "KA2627-I000004"]
        conn.rollback()


def test_numbering_needs_the_right_issuer(conn):
    numbers = DocumentNumbers(fy_start_month=4)
    with pytest.raises(Exception, match="GSTIN"):
        numbers.next(conn, "PO", date(2026, 4, 30))
    with pytest.raises(Exception, match="company-wide"):
        numbers.next(conn, "JV", date(2026, 4, 30), _ka(conn))


# --------------------------------------------------------------------------- run bookkeeping


def test_run_lifecycle_respects_the_table_constraints(conn):
    config = load_generation_config(preset="small", today=date(2026, 11, 10),
                                    overrides={"history.start_date": "2026-05-01", "history.end_date": "2026-05-31"})
    run = RunRecorder.create(conn, build_id=str(uuid.uuid4()), run_mode="HISTORICAL", triggered_by="CLI",
                             base_seed=42, config=config, sim_start=config.start_date, sim_end=config.end_date)
    row = conn.execute(text("SELECT run_number, run_status, config_hash, JSON_EXTRACT(config_snapshot, '$.preset') "
                            "FROM generation_runs WHERE generation_run_id = :r"), {"r": run.run_id}).one()
    assert row[0] == 2 and row[1] == "PENDING" and row[2] == config.config_hash() and row[3] == '"small"'
    steps = conn.execute(text("SELECT step_name FROM generation_run_steps WHERE generation_run_id = :r "
                              "ORDER BY step_order"), {"r": run.run_id}).scalars().all()
    assert tuple(steps) == STEP_NAMES

    run.start(conn)
    with pytest.raises(RunError, match="still RUNNING"):
        RunRecorder.create(conn, build_id=str(uuid.uuid4()), run_mode="CONTINUE", triggered_by="UI",
                           base_seed=42, config=config, sim_start=config.start_date, sim_end=config.end_date)
    run.step_skip(conn, "SCHEMA")
    run.step_start(conn, "DAILY_SIMULATION", planned_units=31)
    run.step_progress(conn, "DAILY_SIMULATION", completed_units=1, generated_rows=812, current_sim_date=date(2026, 5, 1),
                      rows_by_table={"orders": 70, "invoices": 64})
    assert run.day_committed(conn, date(2026, 5, 1)) == datetime(2026, 5, 1, 23, 59, 59, 999999)
    with pytest.raises(RunError, match="outside"):
        run.day_committed(conn, date(2026, 6, 1))
    assert run.cancel_requested(conn) is False
    run.step_finish(conn, "DAILY_SIMULATION")
    with pytest.raises(RunError, match="error message"):
        run.finish(conn, "FAILED", rows_written=812)
    run.finish(conn, "COMPLETED", rows_written=812)
    final = conn.execute(text("SELECT run_status, committed_through, rows_written FROM generation_runs "
                              "WHERE generation_run_id = :r"), {"r": run.run_id}).one()
    assert final == ("COMPLETED", datetime(2026, 5, 1, 23, 59, 59, 999999), 812)
    with pytest.raises(RunError, match="not RUNNING"):
        run.finish(conn, "COMPLETED", rows_written=0)


def test_committed_through_moves_only_with_the_day(engine):
    """A day's data and its committed_through commit (or roll back) together (D64, D67)."""
    config = load_generation_config(preset="small", today=date(2026, 11, 10),
                                    overrides={"history.start_date": "2026-05-01", "history.end_date": "2026-05-31"})
    with engine.connect() as conn:
        run = RunRecorder.create(conn, build_id=str(uuid.uuid4()), run_mode="HISTORICAL", triggered_by="CLI",
                                 base_seed=42, config=config, sim_start=config.start_date, sim_end=config.end_date)
        run.start(conn)
        conn.commit()
        try:
            run.day_committed(conn, date(2026, 5, 1))
            conn.commit()
            run.day_committed(conn, date(2026, 5, 2))
            conn.rollback()                                # day 2 failed
            through = conn.execute(text("SELECT committed_through FROM generation_runs WHERE generation_run_id = :r"),
                                   {"r": run.run_id}).scalar_one()
            assert through == datetime(2026, 5, 1, 23, 59, 59, 999999)
        finally:
            run.finish(conn, "FAILED", rows_written=0, error="test")
            conn.commit()


def _clone(conn, table: str, pk_column: str, pk_value: int, **changes) -> dict:
    """A copy of an existing row (without generated columns) with ``changes`` applied."""
    generated = set(conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
        "AND EXTRA LIKE '%GENERATED%' AND EXTRA NOT LIKE '%DEFAULT_GENERATED%'"), {"s": SCHEMA, "t": table}).scalars())
    row = conn.execute(text(f"SELECT * FROM {table} WHERE {pk_column} = :v"), {"v": pk_value}).mappings().one()
    return {k: v for k, v in row.items() if k not in generated} | changes


@pytest.mark.parametrize("handle_late_links", [True, False])
def test_late_link_is_written_by_update_in_the_same_flush(conn, handle_late_links):
    """D86: a payment and the bank line settling it, buffered together, payment pointing at the line.

    Without the late-link rule the same rows are refused (FK error 1452): the payment table comes first.
    """
    ids = IdAllocator.from_database(conn, SCHEMA)
    writer = BatchWriter.for_schema(conn, SCHEMA)
    if not handle_late_links:
        keys = primary_keys(conn, SCHEMA)
        writer = BatchWriter(dependency_order(conn, SCHEMA), pk_columns={t: k.column for t, k in keys.items()},
                             late_links=frozenset())
    old_payment = conn.execute(text("SELECT MIN(customer_payment_id) FROM customer_payments")).scalar_one()
    old_line = conn.execute(text("SELECT MIN(bank_transaction_id) FROM bank_transactions")).scalar_one()
    payment_id, line_id = ids.next("customer_payments"), ids.next("bank_transactions")
    writer.insert("customer_payments", _clone(
        conn, "customer_payments", "customer_payment_id", old_payment, customer_payment_id=payment_id,
        payment_number="RCPT/KA/2026-27/000099", gateway_reference="UPI0000099",
        settlement_bank_transaction_id=line_id, created_at=NOW, updated_at=NOW))
    writer.insert("bank_transactions", _clone(
        conn, "bank_transactions", "bank_transaction_id", old_line, bank_transaction_id=line_id,
        transaction_type="GATEWAY_SETTLEMENT", customer_payment_id=None, bank_reference="SETL0000099",
        bank_reconciliation_id=None, created_at=NOW, updated_at=NOW))
    if not handle_late_links:
        with pytest.raises(Exception, match="1452"):
            writer.flush(conn)
        return
    writer.flush(conn)
    assert conn.execute(text("SELECT settlement_bank_transaction_id FROM customer_payments WHERE customer_payment_id = :p"),
                        {"p": payment_id}).scalar_one() == line_id
