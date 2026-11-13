"""Generic data-quality checks for the staging copies (1G §9.4, D91), derived from the clean schema's own metadata,
the way dbt's ``not_null`` / ``unique`` / ``relationships`` tests are generated from a model's contract.

The staging copies have no CHECK, UNIQUE or FOREIGN KEY constraints (D90), so these checks are what catches the
damage a source system without constraints can do. Check IDs name the object, so the detection report can match
them with the anomaly registry:

* ``DQ-NOT_NULL:<table>.<column>`` — a column that is NOT NULL in ``_oltp`` has NULLs;
* ``DQ-UNIQUE:<table>.<index>`` — a UNIQUE index of ``_oltp`` (business key) has duplicate values;
* ``DQ-FK:<table>.<column>`` — a foreign-key value with no parent row;
* ``DQ-TIMESTAMP:<table>`` — ``updated_at`` before ``created_at``;
* ``DQ-SCHEMA:<table>`` — the copy's columns differ from the clean table's (a schema change at the source).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass
class DqResult:
    check_id: str
    violations: int
    sample: list = field(default_factory=list)


def _columns(conn: Connection, schema: str) -> dict[str, dict[str, tuple[str, str]]]:
    found: dict[str, dict[str, tuple[str, str]]] = {}
    for table, column, column_type, nullable in conn.execute(text("""
            SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_TYPE, c.IS_NULLABLE FROM information_schema.COLUMNS c
            JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
            WHERE c.TABLE_SCHEMA = :s AND t.TABLE_TYPE = 'BASE TABLE' ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION"""),
            {"s": schema}):
        found.setdefault(table, {})[column] = (column_type, nullable)
    return found


def run_dq(conn: Connection, oltp_schema: str, sim_schema: str, tables: list[str] | None = None) -> list[DqResult]:
    """Every DQ check over the staging copies in ``sim_schema``; returns only the failing ones."""
    clean = _columns(conn, oltp_schema)
    copies = _columns(conn, sim_schema)
    selected = sorted(t for t in clean if t in copies and (tables is None or t in tables))
    failures: list[DqResult] = []

    for table in selected:
        want, have = clean[table], copies[table]
        if {c: t for c, (t, _) in want.items()} != {c: t for c, (t, _) in have.items()}:
            added = sorted(set(have) - set(want))
            dropped = sorted(set(want) - set(have))
            retyped = sorted(c for c in set(want) & set(have) if want[c][0] != have[c][0])
            failures.append(DqResult(f"DQ-SCHEMA:{table}", len(added) + len(dropped) + len(retyped),
                                     [{"added": added, "dropped": dropped, "retyped": retyped}]))
        required = [c for c, (_, nullable) in want.items() if nullable == "NO" and c in have]
        if required:
            counts = conn.execute(text(
                f"SELECT {', '.join(f'SUM(`{c}` IS NULL)' for c in required)} FROM `{sim_schema}`.`{table}`")).fetchone()
            for column, count in zip(required, counts):
                if count:
                    failures.append(DqResult(f"DQ-NOT_NULL:{table}.{column}", int(count)))
        if "created_at" in have and "updated_at" in have:
            count = conn.execute(text(
                f"SELECT COUNT(*) FROM `{sim_schema}`.`{table}` WHERE updated_at < created_at")).scalar()
            if count:
                failures.append(DqResult(f"DQ-TIMESTAMP:{table}", int(count)))

    uniques: dict[tuple[str, str], list[str]] = {}
    for table, index, column in conn.execute(text("""
            SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = :s AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY'
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"""), {"s": oltp_schema}):
        if table in selected:
            uniques.setdefault((table, index), []).append(column)
    for (table, index), columns in sorted(uniques.items()):
        if not set(columns) <= set(copies[table]):
            continue
        listed = ", ".join(f"`{c}`" for c in columns)
        rows = conn.execute(text(
            f"SELECT {listed}, COUNT(*) AS n FROM `{sim_schema}`.`{table}` "
            f"WHERE {' AND '.join(f'`{c}` IS NOT NULL' for c in columns)} GROUP BY {listed} HAVING COUNT(*) > 1 "
            f"LIMIT 20")).fetchall()
        if rows:
            failures.append(DqResult(f"DQ-UNIQUE:{table}.{index}", len(rows), [list(map(str, r)) for r in rows[:5]]))

    for table, column, parent, parent_column in conn.execute(text("""
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL AND ORDINAL_POSITION = 1
            ORDER BY TABLE_NAME, COLUMN_NAME"""), {"s": oltp_schema}).fetchall():
        if table not in selected or parent not in copies or column not in copies[table]:
            continue
        count = conn.execute(text(
            f"SELECT COUNT(*) FROM `{sim_schema}`.`{table}` c LEFT JOIN `{sim_schema}`.`{parent}` p "
            f"ON p.`{parent_column}` = c.`{column}` WHERE c.`{column}` IS NOT NULL AND p.`{parent_column}` IS NULL")).scalar()
        if count:
            failures.append(DqResult(f"DQ-FK:{table}.{column}", int(count)))
    return _dedupe(failures)


def _dedupe(results: list[DqResult]) -> list[DqResult]:
    seen: dict[str, DqResult] = {}
    for result in results:
        if result.check_id in seen:
            seen[result.check_id].violations = max(seen[result.check_id].violations, result.violations)
        else:
            seen[result.check_id] = result
    return list(seen.values())
