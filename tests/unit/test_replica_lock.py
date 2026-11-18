"""A DuckDB file held by another program (e.g. DBeaver) gives a clear ReplicaLockedError naming the holder."""

from __future__ import annotations

import subprocess
import sys
import time

import duckdb
import pytest

from scmgen.loader.replica import ReplicaLockedError, connect_replica

HOLD = "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); print('held', flush=True); time.sleep(60)"


def hold(path):
    """Another process keeps the file open read-write, as a SQL client does."""
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_locked_file_raises_a_clear_error(tmp_path):
    path = tmp_path / "x.duckdb"
    duckdb.connect(str(path)).close()
    proc = hold(path)
    try:
        for read_only in (False, True):
            with pytest.raises(ReplicaLockedError, match=r"(?i)x\.duckdb is open in .*python.*\(process \d+\).*Disconnect"):
                connect_replica(path, read_only=read_only)
    finally:
        proc.kill()
        proc.wait()
    connect_replica(path, read_only=True).close()           # free again once the holder is gone


def test_free_file_opens(tmp_path):
    con = connect_replica(tmp_path / "y.duckdb")
    assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()
