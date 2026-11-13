"""The daily generation engine (1G §2, D64-D68).

``generate_history`` brings a workspace up to a target day:

1. master data on day 0 if the workspace is empty (step 3.5, its own run);
2. one run covering the next uncommitted day to the target day, one MySQL transaction per day:
   the day steps (``steps.DAY_STEPS``) → flush rows → document numbers → ``committed_through`` → commit;
3. validation of the result (D76).

A failed day is rolled back alone (the ID allocator and number series are restored with it); the run is
marked FAILED and the committed days stay. A cancel request takes effect between days. Because every
day starts from committed database state (``World`` is reloaded per run) and draws from day-keyed random
streams (D65), splitting the history across runs gives the same data as one run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from scmgen.core.calendar import days as date_range
from scmgen.core.errors import WorkspaceError
from scmgen.core.rng import RandomStreams
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import (
    ensure_validated,
    record_validation_outcome,
    set_workspace_status,
    workspace_lock,
)
from scmgen.generation.config import GenerationConfig
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.steps import DAY_STEPS
from scmgen.generation.engine.world import load_world
from scmgen.generation.ids import IdAllocator
from scmgen.generation.master import generate_master_data
from scmgen.generation.numbering import DocumentNumbers
from scmgen.generation.packs import load_packs
from scmgen.generation.runs import RunRecorder
from scmgen.generation.seed import load_seed
from scmgen.generation.writer import BatchWriter
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, ValidationReport, run_checks

log = logging.getLogger(__name__)


@dataclass
class HistoryResult:
    run_number: int | None
    first_day: date | None
    last_day: date | None
    days: int
    status: str                                   # COMPLETED / CANCELLED / NOTHING_TO_DO
    rows_by_table: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    report: ValidationReport | None = None
    report_path: str | None = None
    workspace_status: str | None = None
    master_data_rows: int = 0
    cdc_events: int | None = None                 # change events captured after the run (cdc.enabled, D89)


def generate_history(settings: Settings, code: str, config: GenerationConfig, days: int | None = None,
                     force: bool = False, triggered_by: str = "CLI") -> HistoryResult:
    """Simulate from the day after the last committed day up to ``config.end_date`` (or ``days`` days)."""
    workspace = ensure_validated(settings, code, force=force)
    master_rows = 0
    engine = create_mysql_engine(settings, role="app", database=workspace.oltp_schema)
    try:
        with engine.connect() as conn:
            empty = not conn.execute(text("SELECT COUNT(*) FROM companies")).scalar_one()
        if empty:
            master = generate_master_data(settings, code, config, triggered_by=triggered_by)
            master_rows = sum(master.rows_by_table.values())
            if not master.report.passed:
                raise WorkspaceError(f"Master data failed validation ({master.report_path}); daily simulation not started")
            workspace = ensure_validated(settings, code, force=force)
        with engine.connect() as conn:
            last_committed = conn.execute(text("SELECT MAX(committed_through) FROM generation_runs")).scalar_one()
            daily_runs = conn.execute(text("SELECT COUNT(*) FROM generation_runs WHERE run_status <> 'FAILED' "
                                           "AND sim_start_date > (SELECT MIN(sim_start_date) FROM generation_runs)")
                                      ).scalar_one()
    finally:
        engine.dispose()
    if workspace.status != "READY" and not force:
        raise WorkspaceError(f"Workspace '{code}' is {workspace.status}")

    first = last_committed.date() + timedelta(days=1)
    last = config.end_date if days is None else first + timedelta(days=days - 1)
    if not config.allow_future:
        last = min(last, config.end_date)            # simulating today or later needs allow_future (D68)
    if first > last:
        return HistoryResult(None, None, None, 0, "NOTHING_TO_DO", master_data_rows=master_rows,
                             workspace_status=workspace.status)
    result = _run_days(settings, workspace, config, first, last, "CONTINUE" if daily_runs else "HISTORICAL",
                       triggered_by, master_rows)
    _capture_changes(settings, code, result)
    return result


def _capture_changes(settings: Settings, code: str, result: HistoryResult) -> None:
    """With ``cdc.enabled`` (config/simulation.yaml), capture the run's changes into the CDC log (D89).

    Runs after the run committed and passed validation; a capture failure is logged and never undoes the run
    (the next capture picks up from the same keyset position)."""
    from scmgen.simulation.config import load_simulation_config

    if not load_simulation_config().cdc.enabled or not result.days or (result.report and not result.report.passed):
        return
    from scmgen.simulation.cdc import capture
    try:
        result.cdc_events = capture(settings, code).total
        log.info("cdc capture", extra={"event": "cdc.capture", "workspace": code, "events": result.cdc_events})
    except Exception as exc:                       # pragma: no cover - logged, the run itself stands
        log.error("cdc capture failed: %s", exc, extra={"event": "cdc.capture.error", "workspace": code})


def _run_days(settings: Settings, workspace, config: GenerationConfig, first: date, last: date, mode: str,
              triggered_by: str, master_rows: int) -> HistoryResult:
    seed = load_seed()
    packs = load_packs(workspace.industries, seed)
    code = workspace.code
    control = create_mysql_engine(settings, role="app")
    engine = create_mysql_engine(settings, role="app", database=workspace.oltp_schema)
    total_days = (last - first).days + 1
    status = "COMPLETED"
    started = time.perf_counter()
    try:
        with control.connect() as lock_conn, workspace_lock(lock_conn, code), engine.connect() as conn:
            run = RunRecorder.create(conn, build_id=workspace.build_id, run_mode=mode, triggered_by=triggered_by,
                                     base_seed=workspace.base_seed, config=config, sim_start=first, sim_end=last)
            run.start(conn)
            world = load_world(conn, config, seed, packs, code, workspace.company_name)
            opening_due = world.first_day == first
            for step in ("SCHEMA", "SEED_DATA", "MASTER_DATA") + (() if opening_due else ("OPENING_BALANCES",)):
                run.step_skip(conn, step)
            if opening_due:
                run.step_start(conn, "OPENING_BALANCES", planned_units=1)
            run.step_start(conn, "DAILY_SIMULATION", planned_units=total_days)
            conn.commit()
            run_number = conn.execute(text("SELECT run_number FROM generation_runs WHERE generation_run_id = :r"),
                                      {"r": run.run_id}).scalar_one()
            set_workspace_status(settings, code, "GENERATING")

            ids = IdAllocator.from_database(conn, workspace.oltp_schema)
            writer = BatchWriter.for_schema(conn, workspace.oltp_schema, config.batch_size)
            numbers = DocumentNumbers(config.fiscal_year_start_month)
            streams = RandomStreams(workspace.base_seed)
            committed = None
            done = 0
            for current in date_range(first, last):
                if run.cancel_requested(conn):
                    status = "CANCELLED"
                    break
                checkpoint = ids.checkpoint()
                try:
                    day = DayContext(current, conn, world, ids, writer, numbers, streams, run.run_id)
                    for _, step in DAY_STEPS:
                        step(day)
                    writer.flush(conn)
                    numbers.flush(conn)
                    committed = run.day_committed(conn, current)
                    if opening_due and done == 0:
                        run.step_progress(conn, "OPENING_BALANCES", completed_units=1,
                                          generated_rows=sum(writer.written.values()), current_sim_date=current)
                        run.step_finish(conn, "OPENING_BALANCES")
                    done += 1
                    if done % config.daily.progress_every_days == 0 or current == last:
                        elapsed = max(time.perf_counter() - started, 1e-6)
                        rows = sum(writer.written.values())
                        run.step_progress(conn, "DAILY_SIMULATION", completed_units=done, generated_rows=rows,
                                          current_sim_date=current, rows_by_table=dict(writer.written),
                                          rows_per_second=Decimal(rows / elapsed).quantize(Decimal("0.01")))
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    ids.restore(checkpoint)
                    writer.discard()
                    numbers.discard()
                    run.step_finish(conn, "DAILY_SIMULATION", "FAILED")
                    if opening_due and done == 0:
                        run.step_finish(conn, "OPENING_BALANCES", "FAILED")
                    run.finish(conn, "FAILED", rows_written=sum(writer.written.values()),
                               error=f"{current}: {type(exc).__name__}: {exc}"[:2000])
                    conn.commit()
                    set_workspace_status(settings, code, "READY", last_committed_through=committed)
                    log.error("day failed", extra={"event": "engine.day_failed", "workspace": code, "day": str(current)})
                    raise

            run.step_finish(conn, "DAILY_SIMULATION")         # also when cancelled: the committed days are complete
            run.step_start(conn, "VALIDATION")
            conn.commit()
            report = run_checks(ValidationContext(conn, workspace_code=code), load_catalogue(),
                                schema=workspace.oltp_schema)
            conn.rollback()
            path = report.write(settings.data_dir / "reports" / code, f"run_{run_number}_validation")
            root = settings.data_dir.parent
            relative = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            run.record_validation(conn, report.passed, relative)
            run.step_finish(conn, "VALIDATION")
            run.step_start(conn, "FINALISE")
            run.step_finish(conn, "FINALISE")
            run.finish(conn, status, rows_written=sum(writer.written.values()))
            conn.commit()
    finally:
        engine.dispose()
        control.dispose()

    set_workspace_status(settings, code, "READY", last_committed_through=committed)
    workspace_status = record_validation_outcome(settings, code, report.passed)
    seconds = time.perf_counter() - started
    log.info("history generated", extra={"event": "engine.run", "workspace": code, "days": done,
                                         "status": status, "seconds": round(seconds, 2)})
    last_done = first + timedelta(days=done - 1) if done else None
    return HistoryResult(run_number, first, last_done, done, status, dict(writer.written), seconds, report, relative,
                         workspace_status, master_rows)
