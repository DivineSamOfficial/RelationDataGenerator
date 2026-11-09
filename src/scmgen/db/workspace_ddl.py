"""Apply the workspace schema DDL (all business tables) to a target MySQL schema.

Files live in scmgen/db/ddl/workspace/ and are applied in file-name order, which is
foreign-key dependency order (NNNN_ prefixes). Tables are created with plain CREATE TABLE
(no IF NOT EXISTS) on purpose: a workspace schema is always built from empty, so a
half-existing schema fails loudly instead of being silently patched.
"""

from __future__ import annotations

import logging
import re
from importlib import resources

from sqlalchemy import Connection

from scmgen.core.errors import ConfigError
from scmgen.db.sql_files import DDL_PACKAGE, execute_raw, read_sql, run_sql

log = logging.getLogger(__name__)

WORKSPACE_DDL_DIR = "ddl/workspace"
_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def workspace_ddl_files() -> list[str]:
    """Relative paths of the workspace DDL files in application order."""
    directory = resources.files(DDL_PACKAGE) / WORKSPACE_DDL_DIR
    names = sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".sql"))
    return [f"{WORKSPACE_DDL_DIR}/{name}" for name in names]


def apply_workspace_ddl(conn: Connection, schema: str) -> int:
    """Create every workspace table in `schema` (which must exist and be empty).

    Returns the number of statements executed."""
    if not _SCHEMA_RE.match(schema):
        raise ConfigError(f"Unsafe schema name: {schema!r}")
    execute_raw(conn, f"USE `{schema}`")
    executed = 0
    for path in workspace_ddl_files():
        count = run_sql(conn, read_sql(path))
        executed += count
        log.debug("applied ddl file", extra={"event": "ddl.apply", "file": path, "statements": count, "schema": schema})
    return executed
