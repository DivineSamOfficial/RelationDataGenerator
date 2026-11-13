"""Re-run actions on a workspace: Continue, Rebuild, Plan (D14, D68, D76; 1G §3-4; step 3.16).

* **Continue** advances an existing history: the same preset and overrides as the workspace's last run (read from
  its ``config_snapshot``), the original start date kept (the engine keys streams and windows on it), and the end
  moved to yesterday (D68) or ``days`` days further; ``allow_future`` lets it run past today. Refused when the last
  validation failed, unless forced (D76).
* **Rebuild** drops and recreates the workspace with the same company, industries and seed (new ``build_id``) and
  regenerates the same history (same preset, overrides and end date): same seed ⇒ the same data (V-SY-04).
* **Plan** says what Continue would do, without writing anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import create_workspace, delete_workspace, get_workspace
from scmgen.generation.config import GenerationConfig, load_generation_config
from scmgen.generation.engine import HistoryResult, generate_history


@dataclass(frozen=True)
class LastRun:
    preset: str
    overrides: dict[str, Any]
    start_date: date
    committed_through: date | None
    last_status: str | None


def last_run(settings: Settings, code: str) -> LastRun:
    """The configuration of the workspace's last daily run and how far it has committed."""
    workspace = get_workspace(settings, code)
    engine = create_mysql_engine(settings, role="app", database=workspace.oltp_schema)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT config_snapshot, run_status FROM generation_runs ORDER BY generation_run_id DESC LIMIT 1""")).first()
            committed = conn.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar()
    finally:
        engine.dispose()
    if row is None:
        raise WorkspaceError(f"Workspace '{code}' has no generation run yet: run `generate history` first")
    snapshot = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return LastRun(snapshot["preset"], dict(snapshot.get("overrides") or {}), date.fromisoformat(snapshot["start_date"]),
                   committed.date() if committed else None, row[1])


def continue_config(settings: Settings, code: str, days: int | None = None, allow_future: bool = False,
                    today: date | None = None) -> GenerationConfig:
    """The configuration Continue runs with (D68): the last run's, with the end moved forward."""
    run = last_run(settings, code)
    overrides = {k: v for k, v in run.overrides.items() if k not in ("history.end_date", "history.allow_future")}
    overrides["history.start_date"] = run.start_date.isoformat()
    if days is not None and run.committed_through is not None:
        end = run.committed_through + timedelta(days=days)
        overrides["history.end_date"] = end.isoformat()
        overrides["history.allow_future"] = allow_future
    elif allow_future:
        overrides["history.allow_future"] = True
    return load_generation_config(preset=run.preset, overrides=overrides, today=today)


def continue_workspace(settings: Settings, code: str, days: int | None = None, allow_future: bool = False,
                       force: bool = False, triggered_by: str = "CLI") -> HistoryResult:
    config = continue_config(settings, code, days=days, allow_future=allow_future)
    return generate_history(settings, code, config, days=days, force=force, triggered_by=triggered_by)


def rebuild_workspace(settings: Settings, code: str, confirm: str, triggered_by: str = "CLI") -> HistoryResult:
    """Drop, recreate and regenerate the same history (requires typing the workspace code, 1G §4)."""
    if confirm != code:
        raise WorkspaceError(f"Rebuild drops every row of '{code}': confirm by passing the workspace code")
    workspace = get_workspace(settings, code)
    run = last_run(settings, code)
    overrides = dict(run.overrides)
    overrides["history.start_date"] = run.start_date.isoformat()
    if run.committed_through is not None:
        overrides["history.end_date"] = run.committed_through.isoformat()
        overrides["history.allow_future"] = True          # the same end even if it is in the past by now
    config = load_generation_config(preset=run.preset, overrides=overrides)
    delete_workspace(settings, code, purge=True, keep_replicas=True)   # the replica full-reloads on the new build
    create_workspace(settings, code, workspace.company_name, list(workspace.industries), base_seed=workspace.base_seed)
    return generate_history(settings, code, config, triggered_by=triggered_by)


@dataclass(frozen=True)
class Plan:
    code: str
    status: str
    preset: str
    overrides: dict[str, Any]
    first_day: date | None
    last_day: date | None
    days: int
    blocked: str | None


def plan(settings: Settings, code: str, days: int | None = None, allow_future: bool = False) -> Plan:
    """What Continue would simulate, without writing anything."""
    workspace = get_workspace(settings, code)
    run = last_run(settings, code)
    config = continue_config(settings, code, days=days, allow_future=allow_future)
    first = (run.committed_through + timedelta(days=1)) if run.committed_through else config.start_date
    last = config.end_date if days is None else first + timedelta(days=days - 1)
    if not config.allow_future:
        last = min(last, config.end_date)
    blocked = None
    if workspace.status == "NEEDS_ATTENTION":
        blocked = "the last run failed validation (D76): fix it or run with --force"
    elif workspace.status != "READY":
        blocked = f"the workspace is {workspace.status}"
    count = max(0, (last - first).days + 1)
    return Plan(code, workspace.status, run.preset, run.overrides, first if count else None, last if count else None,
                count, blocked)
