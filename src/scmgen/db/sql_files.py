"""Read packaged .sql files and run them statement by statement.

Our DDL contains no stored procedures or triggers, so statements end at ';'. The splitter
ignores ';' inside quoted text ('...', "...", `...`), e.g. in a table COMMENT, and drops
full-line '--' comments first.
"""

from __future__ import annotations

from importlib import resources

from sqlalchemy import Connection

DDL_PACKAGE = "scmgen.db"


def read_sql(relative_path: str) -> str:
    """Read e.g. 'ddl/control/0001_workspaces.sql' from the installed package."""
    return (resources.files(DDL_PACKAGE) / relative_path).read_text(encoding="utf-8")


def split_statements(sql: str) -> list[str]:
    text = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        current.append(char)
        if quote:
            if char == "\\" and quote != "`" and i + 1 < len(text):  # backslash escape inside '...' / "..."
                current.append(text[i + 1])
                i += 1
            elif char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:  # doubled quote = literal quote
                    current.append(text[i + 1])
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == ";":
            current.pop()
            statements.append("".join(current))
            current = []
        i += 1
    statements.append("".join(current))
    return [stmt.strip() for stmt in statements if stmt.strip()]


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
