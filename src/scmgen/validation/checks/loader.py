"""Loader rules (1G §12 V-LD-*; Phase 4). Each check returns the violating rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scmgen.validation.framework import NotApplicable, ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-LD-02")
def append_tables_are_insert_only(ctx: ValidationContext) -> Rows:
    """APPEND tables have no source rows with ``updated_at > created_at`` (1G §6.4): a table loaded on its
    ``created_at`` watermark would otherwise lose updates in the replica."""
    from scmgen.loader.config import load_loader_config
    found: Rows = []
    for table, strategy in sorted(load_loader_config().strategies.items()):
        if strategy == "APPEND":
            changed = ctx.scalar(f"SELECT COUNT(*) FROM `{table}` WHERE updated_at > created_at")
            if changed:
                found.append({"table": table, "rows_updated_after_insert": changed,
                              "problem": "insert-only table has updated rows: reclassify it as MERGE"})
    return found


@check("V-LD-01")
def replica_matches_source(ctx: ValidationContext) -> Rows:
    """★ The DuckDB replica holds, for every incremental table, exactly the source rows created up to the
    replica's high-water mark, without duplicate keys (1G §6.6; the loader also compares sums after each load)."""
    import duckdb  # noqa: F401  (duckdb.Error below)
    from scmgen.core.settings import load_settings
    from scmgen.db.workspaces import get_workspace
    from scmgen.loader.config import load_loader_config
    if not ctx.workspace_code:
        raise NotApplicable("no workspace context")
    workspace = get_workspace(load_settings(), ctx.workspace_code)
    path = Path(workspace.duckdb_path)
    if not path.exists():
        raise NotApplicable(f"no DuckDB replica yet ({path.name}): run `scmgen load`")
    from scmgen.loader.replica import ReplicaLockedError, connect_replica
    try:
        con = connect_replica(path, read_only=True)
    except ReplicaLockedError as exc:
        # The replica can't be read right now; the clean data isn't wrong, so this must not block the workspace
        # (D76). Every load reconciles the replica anyway, and validation can be re-run once the file is free.
        raise NotApplicable(f"replica not checked: {exc}") from exc
    try:
        high = con.execute("SELECT info_value FROM etl_meta.replica_info WHERE info_key = 'high_water_mark'").fetchone()
        if high is None:
            raise NotApplicable("the replica has not completed a load yet")
        build = con.execute("SELECT info_value FROM etl_meta.replica_info WHERE info_key = 'source_build_id'").fetchone()
        if build is None or build[0] != workspace.build_id:
            raise NotApplicable("the replica holds an earlier build of the workspace: the next load reloads it in full")
        found: Rows = []
        for table, strategy in sorted(load_loader_config().strategies.items()):
            if strategy == "FULL":
                continue                         # reloaded whole every time; the source may have moved on since
            pk = ctx.scalar("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
                            "AND TABLE_NAME = :t AND CONSTRAINT_NAME = 'PRIMARY'", t=table)
            source = ctx.scalar(f"SELECT COUNT(*) FROM `{table}` WHERE created_at <= :h", h=high[0])
            try:
                rows, keys = con.execute(f'SELECT COUNT(*), COUNT(DISTINCT "{pk}") FROM raw."{table}"').fetchone()
            except duckdb.Error:
                rows = keys = None
            if rows is None or rows != source or keys != rows:
                found.append({"table": table, "source_rows": source, "replica_rows": rows, "replica_keys": keys,
                              "problem": "replica rows differ from the source up to the high-water mark"})
        return found
    finally:
        con.close()
