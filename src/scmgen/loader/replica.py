"""Opening a DuckDB replica file, with a clear error when another program holds it.

DuckDB lets one process write a database file, or several processes read it, never both. A SQL client (DBeaver,
the DuckDB CLI, a notebook) that keeps a replica open therefore blocks every load and CDC apply (and, if it opened
the file read-write, every replica check) until it disconnects. DuckDB's own error names the holder; ``connect_replica`` turns it into
``ReplicaLockedError`` with what to do, so callers can report it plainly (or, in validation, skip the replica
comparison instead of flagging the clean data, which isn't wrong).
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from scmgen.core.errors import ScmgenError

_HOLDER = re.compile(r"Conflicting lock is held in (?P<program>.+?) \(PID (?P<pid>\d+)\)")


class ReplicaLockedError(ScmgenError):
    """Another program holds the DuckDB file."""


def connect_replica(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    try:
        return duckdb.connect(str(path), read_only=read_only)
    except duckdb.IOException as exc:
        if "lock" not in str(exc).lower():
            raise
        found = _HOLDER.search(str(exc))
        holder = f"{Path(found['program']).name} (process {found['pid']})" if found else "another program"
        raise ReplicaLockedError(
            f"{path.name} is open in {holder}. A DuckDB file can't be written while another program has it "
            f"open: disconnect it there (in DBeaver: right-click the connection → Disconnect) and try "
            f"again.") from exc
