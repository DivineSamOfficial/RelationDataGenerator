"""Document numbers (1C §2.1, D38) issued gap-free inside the day transaction (D75).

Formats (each one is also a CHECK in the DDL):

* readable documents ``<PREFIX>/<STATE>/<FY>/<SEQ6>``        ``PO/KA/2026-27/000123``
* GST tax documents  ``<STATE><FY4>-<LETTER><SEQ6>`` (≤ 16)  ``KA2627-I000123``
* journals           ``JV/<FY>/<SEQ7>`` (company-wide)       ``JV/2026-27/0000042``

A series is one ``document_sequences`` row per document type × issuing GSTIN × financial year.
The first use of a series in a day reads (and locks) its row; numbers are then counted in memory and
written back by ``flush()`` just before the day commits. If the day rolls back, ``discard()`` forgets
them, the database row was never changed, and the next attempt issues the same numbers: committed tax
documents stay consecutive with no gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import fiscal_year, fiscal_year_compact, fiscal_year_label, wall_clock


class NumberingError(RuntimeError):
    """A number can't be issued (unknown type, missing GSTIN, exhausted series)."""


@dataclass(frozen=True)
class Series:
    style: str            # "standard" | "tax" | "journal"
    code: str             # prefix for standard/journal, letter for tax documents
    digits: int


DOCUMENT_SERIES: dict[str, Series] = {
    # Procurement and inventory (1C §2.1)
    "RFQ": Series("standard", "RFQ", 6),
    "QTN": Series("standard", "QTN", 6),
    "PO": Series("standard", "PO", 6),
    "GRN": Series("standard", "GRN", 6),
    "SPAY": Series("standard", "SPAY", 6),
    "ADJ": Series("standard", "ADJ", 6),
    "STO": Series("standard", "STO", 6),
    # Sales, shipping, returns (1D)
    "SO": Series("standard", "SO", 6),
    "RCPT": Series("standard", "RCPT", 6),
    "SHP": Series("standard", "SHP", 6),
    "RMA": Series("standard", "RMA", 6),
    "RFD": Series("standard", "RFD", 6),
    "DEF": Series("standard", "DEF", 6),
    # Expenses (1E)
    "EXP": Series("standard", "EXP", 6),
    "EPAY": Series("standard", "EPAY", 6),
    # GST tax documents (D38)
    "TAX_INVOICE": Series("tax", "I", 6),
    "CREDIT_NOTE": Series("tax", "C", 6),
    "TRANSFER_INVOICE": Series("tax", "T", 6),
    # Journals (1E §3)
    "JV": Series("journal", "JV", 7),
}


@dataclass(frozen=True)
class Registration:
    """The issuing GSTIN of a document: its id and the 2-letter state code of its state."""

    company_gst_registration_id: int
    state_code: str


@dataclass
class _Counter:
    registration_id: int | None
    fiscal_year: int
    last: int
    changed: bool = False


class DocumentNumbers:
    def __init__(self, fy_start_month: int) -> None:
        self.fy_start_month = fy_start_month
        self._counters: dict[tuple[str, int, int], _Counter] = {}

    def next(self, conn: Connection, document_type: str, document_date: date,
             registration: Registration | None = None) -> str:
        series = DOCUMENT_SERIES.get(document_type)
        if series is None:
            raise NumberingError(f"Unknown document type {document_type!r}")
        if series.style == "journal":
            if registration is not None:
                raise NumberingError("Journal numbers are company-wide; don't pass a GSTIN")
        elif registration is None:
            raise NumberingError(f"{document_type} numbers need the issuing GSTIN")

        year = fiscal_year(document_date, self.fy_start_month)
        registration_id = registration.company_gst_registration_id if registration else None
        key = (document_type, registration_id or 0, year)
        counter = self._counters.get(key)
        if counter is None:
            counter = _Counter(registration_id, year, self._load(conn, key))
            self._counters[key] = counter
        if counter.last + 1 >= 10 ** series.digits:
            raise NumberingError(f"{document_type} series for FY {year} is exhausted ({series.digits} digits)")
        counter.last += 1
        counter.changed = True
        return self.format(document_type, year, counter.last, registration.state_code if registration else None)

    def format(self, document_type: str, year: int, sequence: int, state_code: str | None) -> str:
        series = DOCUMENT_SERIES[document_type]
        seq = f"{sequence:0{series.digits}d}"
        if series.style == "journal":
            return f"{series.code}/{fiscal_year_label(year, self.fy_start_month)}/{seq}"
        if not state_code or len(state_code) != 2 or not state_code.isalpha() or not state_code.isupper():
            raise NumberingError(f"Invalid state code {state_code!r}")
        if series.style == "tax":
            return f"{state_code}{fiscal_year_compact(year)}-{series.code}{seq}"
        return f"{series.code}/{state_code}/{fiscal_year_label(year, self.fy_start_month)}/{seq}"

    @staticmethod
    def _load(conn: Connection, key: tuple[str, int, int]) -> int:
        document_type, series_key, year = key
        value = conn.execute(text(
            "SELECT last_sequence FROM document_sequences "
            "WHERE document_type = :t AND series_key = :k AND fiscal_year = :y FOR UPDATE"),
            {"t": document_type, "k": series_key, "y": year}).scalar()
        return int(value or 0)

    def flush(self, conn: Connection) -> int:
        """Write every series used since the last flush (call inside the day transaction, before commit).

        ``document_sequences`` is a system table, so its audit columns take the wall clock (D74).
        """
        now = wall_clock()
        rows = [
            {"t": document_type, "r": counter.registration_id, "y": counter.fiscal_year,
             "n": counter.last, "now": now}
            for (document_type, _, _), counter in self._counters.items() if counter.changed
        ]
        if rows:
            conn.execute(text(
                "INSERT INTO document_sequences "
                "(document_type, company_gst_registration_id, fiscal_year, last_sequence, created_at, updated_at) "
                "VALUES (:t, :r, :y, :n, :now, :now) AS new "
                "ON DUPLICATE KEY UPDATE last_sequence = new.last_sequence, updated_at = new.updated_at"), rows)
        self._counters.clear()
        return len(rows)

    def discard(self) -> None:
        """Forget numbers issued in a day that is being rolled back."""
        self._counters.clear()
