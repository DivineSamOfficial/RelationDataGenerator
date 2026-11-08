"""Read packaged .sql files and run them statement by statement.

The splitter is deliberately simple: our DDL files contain no stored procedures,
triggers or semicolons inside string literals, so 'split on ;' is correct. Full-line
'--' comments are removed first.
"""

from __future__ import annotations

from importlib import resources

from sqlalchemy import Connection

DDL_PACKAGE = "scmgen.db"


def read_sql(relative_path: str) -> str:
    """Read e.g. 'ddl/control/0001_workspaces.sql' from the installed package."""
    return (resources.files(DDL_PACKAGE) / relative_path).read_text(encoding="utf-8")


def split_statements(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def execute_raw(conn: Connection, statement: str) -> None:
    """Run one statement on the DBAPI cursor with *no* parameters.

    Needed for any SQL containing '%' (GRANT patterns like `scm\\_%`, LIKE literals):
    SQLAlchemy's exec_driver_sql still passes an empty parameter set, and PyMySQL then
    applies Python %-formatting to the statement, which breaks on a bare '%'.
    Calling cursor.execute(sql) without args skips that formatting entirely.
    """
    cursor = conn.connection.cursor()
    try:
        cursor.execute(statement)
    finally:
        cursor.close()


def run_sql(conn: Connection, sql: str) -> int:
    """Execute every statement in a script; returns how many ran."""
    statements = split_statements(sql)
    for statement in statements:
        execute_raw(conn, statement)
    return len(statements)
