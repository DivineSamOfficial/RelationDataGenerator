"""System rules (1G §12, V-SY-*).

V-SY-04 (determinism) is a double-build test, not a check on one database: step 3.16.
V-SY-03 covers audit timestamps here; its "no future business dates" part needs the list of event-date
columns that the transaction steps define, and is added with the daily engine (step 3.6).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]

SYSTEM_TABLES = frozenset({"document_sequences", "generation_runs", "generation_run_steps"})
TAX_SERIES = frozenset({"TAX_INVOICE", "CREDIT_NOTE", "TRANSFER_INVOICE"})

# Where each series' numbers are stored: (document type, table, column).
NUMBERED_COLUMNS = (
    ("RFQ", "rfq_headers", "rfq_number"), ("QTN", "quotation_headers", "quotation_number"),
    ("PO", "purchase_order_headers", "po_number"), ("GRN", "goods_receipts", "grn_number"),
    ("SPAY", "supplier_payments", "payment_number"), ("ADJ", "stock_adjustments", "adjustment_number"),
    ("STO", "stock_transfers", "transfer_number"), ("TRANSFER_INVOICE", "stock_transfers", "tax_invoice_number"),
    ("SO", "orders", "order_number"), ("RCPT", "customer_payments", "payment_number"),
    ("SHP", "shipments", "shipment_number"), ("RMA", "sales_returns", "return_number"),
    ("RFD", "refunds", "refund_number"), ("DEF", "defective_stock", "defect_number"),
    ("EXP", "expenses", "expense_number"), ("EPAY", "expense_payments", "payment_number"),
    ("TAX_INVOICE", "invoices", "invoice_number"), ("CREDIT_NOTE", "credit_notes", "credit_note_number"),
    ("JV", "journal_entries", "journal_number"),
)
_STANDARD = re.compile(r"^[A-Z]+/([A-Z]{2})/([0-9]{4})-[0-9]{2}/([0-9]{6})$")
_TAX = re.compile(r"^([A-Z]{2})([0-9]{2})[0-9]{2}-[ICT]([0-9]{6})$")
_JOURNAL = re.compile(r"^JV/([0-9]{4})-[0-9]{2}/([0-9]{7})$")


def _series_key(document_type: str, number: str) -> tuple[tuple[str, str | None, int], int] | None:
    """(document type, state code or None, FY start year) and the sequence number, from a document number."""
    if document_type == "JV":
        match = _JOURNAL.match(number)
        return ((document_type, None, int(match.group(1))), int(match.group(2))) if match else None
    if document_type in TAX_SERIES:
        match = _TAX.match(number)
        return ((document_type, match.group(1), 2000 + int(match.group(2))), int(match.group(3))) if match else None
    match = _STANDARD.match(number)
    return ((document_type, match.group(1), int(match.group(2))), int(match.group(3))) if match else None


@check("V-SY-01")
def sequences_match_issued_numbers(ctx: ValidationContext) -> Rows:
    """last_sequence = highest number issued per series; tax-document series have no gaps (D75)."""
    issued: dict[tuple[str, str | None, int], list[int]] = defaultdict(list)
    found: Rows = []
    for document_type, table, column in NUMBERED_COLUMNS:
        for (number,) in ctx.conn.exec_driver_sql(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
            parsed = _series_key(document_type, number)
            if parsed is None:
                found.append({"document_type": document_type, "number": number, "problem": "unparseable number"})
                continue
            issued[parsed[0]].append(parsed[1])
    sequences = {(r["document_type"], r["state_code"], r["fiscal_year"]): r["last_sequence"] for r in ctx.rows("""
        SELECT d.document_type, s.state_code, d.fiscal_year, d.last_sequence
        FROM document_sequences d
        LEFT JOIN company_gst_registrations r ON r.company_gst_registration_id = d.company_gst_registration_id
        LEFT JOIN states s ON s.state_id = r.state_id""")}
    for key in sorted(set(issued) | set(sequences), key=lambda k: (k[0], k[1] or "", k[2])):
        numbers = issued.get(key, [])
        highest = max(numbers, default=0)
        series = {"document_type": key[0], "state_code": key[1], "fiscal_year": key[2]}
        if sequences.get(key) != highest:
            found.append({**series, "last_sequence": sequences.get(key), "highest_issued": highest,
                          "problem": "last_sequence differs from the highest issued number"})
        if key[0] in TAX_SERIES and numbers and len(set(numbers)) != highest:
            found.append({**series, "issued": len(set(numbers)), "highest_issued": highest,
                          "problem": "gap in a GST tax-document series"})
    return found


@check("V-SY-02")
def runs_contiguous(ctx: ValidationContext) -> Rows:
    """Runs of a build follow each other in simulated time; committed_through only increases; <= 1 RUNNING."""
    found: Rows = []
    running = ctx.scalar("SELECT COUNT(*) FROM generation_runs WHERE run_status = 'RUNNING'")
    if running > 1:
        found.append({"problem": f"{running} runs are RUNNING (at most one may be)"})
    by_build: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.rows("SELECT run_number, build_id, sim_start_date, committed_through FROM generation_runs "
                      "ORDER BY build_id, run_number"):
        by_build[r["build_id"]].append(r)
    for runs in by_build.values():
        last_committed = None
        for run in runs:
            if last_committed is not None:
                expected = last_committed.date().toordinal() + 1
                if run["sim_start_date"].toordinal() != expected:
                    found.append({"run_number": run["run_number"], "sim_start_date": run["sim_start_date"],
                                  "previous_committed_through": last_committed,
                                  "problem": "does not start the day after the previous committed day"})
                if run["committed_through"] is not None and run["committed_through"] < last_committed:
                    found.append({"run_number": run["run_number"], "problem": "committed_through moved backwards"})
            if run["committed_through"] is not None:
                last_committed = run["committed_through"]
    return found


@check("V-SY-03")
def audit_timestamps_within_committed_time(ctx: ValidationContext) -> Rows:
    """Business rows are recorded no later than the last committed simulated time (D67), and rows a run
    wrote (ledger movements, journals) are not stamped before that run's first simulated day."""
    found: Rows = []
    limit = ctx.scalar("SELECT MAX(committed_through) FROM generation_runs")
    tables = [t for (t,) in ctx.conn.exec_driver_sql(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")
        if t not in SYSTEM_TABLES]
    for table in sorted(tables):
        if limit is None:
            rows = ctx.scalar(f"SELECT COUNT(*) FROM {table}")
            if rows:
                found.append({"table": table, "rows": rows, "problem": "data exists but no run committed any day"})
            continue
        late = ctx.scalar(f"SELECT COUNT(*) FROM {table} WHERE updated_at > :limit", limit=limit)
        if late:
            found.append({"table": table, "rows": late, "committed_through": limit,
                          "problem": "updated_at after the last committed simulated time"})
    for table in ("inventory_transactions", "journal_entries"):
        early = ctx.scalar(f"""
            SELECT COUNT(*) FROM {table} t JOIN generation_runs g ON g.generation_run_id = t.generation_run_id
            WHERE t.created_at < g.sim_start_date""")
        if early:
            found.append({"table": table, "rows": early, "problem": "created before its run's first simulated day"})
    return found
