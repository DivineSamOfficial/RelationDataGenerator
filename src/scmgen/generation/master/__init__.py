"""Master-data generation (Phase 3 step 3.5): fills an empty workspace on day 0, the setup day.

``generate_master_data`` records a generation run whose window is the setup day only (the day before
the first simulated day), builds every master table in one transaction, commits it together with
``committed_through`` (D64/D67), validates the result (D76) and records the outcome. The historical
daily simulation (step 3.6) then starts the next day, so runs stay contiguous (V-SY-02).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text

from scmgen.core.errors import WorkspaceError
from scmgen.core.rng import RandomStreams
from scmgen.core.settings import Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import get_workspace, record_validation_outcome, set_workspace_status, workspace_lock
from scmgen.generation.config import GenerationConfig
from scmgen.generation.ids import IdAllocator
from scmgen.generation.master.context import MasterContext
from scmgen.generation.master.organisation import build_organisation
from scmgen.generation.packs import load_packs
from scmgen.generation.runs import RunRecorder
from scmgen.generation.seed import load_seed
from scmgen.generation.writer import BatchWriter
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, ValidationReport, run_checks

log = logging.getLogger(__name__)

BUILDERS = (build_organisation,)


@dataclass(frozen=True)
class MasterDataResult:
    run_number: int
    setup_day: str
    rows_by_table: dict[str, int]
    seconds: float
    report: ValidationReport
    report_path: str
    workspace_status: str


def generate_master_data(settings: Settings, code: str, config: GenerationConfig,
                         triggered_by: str = "CLI") -> MasterDataResult:
    workspace = get_workspace(settings, code)
    if workspace.status != "READY":
        raise WorkspaceError(f"Workspace '{code}' is {workspace.status}; master data needs a READY workspace")
    seed = load_seed()
    packs = load_packs(workspace.industries, seed)
    setup_day = config.start_date - timedelta(days=1)

    control = create_mysql_engine(settings, role="app")
    engine = create_mysql_engine(settings, role="app", database=workspace.oltp_schema)
    try:
        with control.connect() as lock_conn, workspace_lock(lock_conn, code), engine.connect() as conn:
            if conn.execute(text("SELECT COUNT(*) FROM companies")).scalar_one():
                raise WorkspaceError(f"Workspace '{code}' already has master data. Rebuild it to start again (D14)")
            run = RunRecorder.create(conn, build_id=workspace.build_id, run_mode="HISTORICAL",
                                     triggered_by=triggered_by, base_seed=workspace.base_seed, config=config,
                                     sim_start=setup_day, sim_end=setup_day)
            run.start(conn)
            run.step_skip(conn, "SCHEMA")
            conn.commit()
            run_number = conn.execute(text("SELECT run_number FROM generation_runs WHERE generation_run_id = :r"),
                                      {"r": run.run_id}).scalar_one()
            set_workspace_status(settings, code, "GENERATING")
            started = time.perf_counter()
            try:
                ids = IdAllocator.from_database(conn, workspace.oltp_schema)
                writer = BatchWriter.for_schema(conn, workspace.oltp_schema, config.batch_size)
                ctx = MasterContext(config=config, seed=seed, packs=packs, streams=RandomStreams(workspace.base_seed),
                                    ids=ids, writer=writer, workspace_code=code,
                                    company_name=workspace.company_name, setup_day=setup_day)
                run.step_start(conn, "SEED_DATA")
                run.step_finish(conn, "SEED_DATA")
                run.step_start(conn, "MASTER_DATA")
                for builder in BUILDERS:
                    builder(ctx)
                rows = writer.flush(conn)
                total = sum(rows.values())
                run.step_progress(conn, "MASTER_DATA", completed_units=1, generated_rows=total,
                                  current_sim_date=setup_day, rows_by_table=rows)
                run.step_finish(conn, "MASTER_DATA")
                for step in ("OPENING_BALANCES", "DAILY_SIMULATION"):
                    run.step_skip(conn, step)
                committed = run.day_committed(conn, setup_day)
                conn.commit()                                      # master data + committed_through together
            except Exception as exc:
                conn.rollback()
                run.finish(conn, "FAILED", rows_written=0, error=f"{type(exc).__name__}: {exc}"[:2000])
                conn.commit()
                set_workspace_status(settings, code, "READY")
                raise

            try:
                run.step_start(conn, "VALIDATION")
                conn.commit()
                report = run_checks(ValidationContext(conn, workspace_code=code), load_catalogue(),
                                    schema=workspace.oltp_schema)
                conn.rollback()                                    # validation only reads
                path = report.write(settings.data_dir / "reports" / code, f"run_{run_number}_validation")
                root = settings.data_dir.parent
                relative = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
                run.record_validation(conn, report.passed, relative)
                run.step_finish(conn, "VALIDATION")
                run.step_start(conn, "FINALISE")
                run.step_finish(conn, "FINALISE")
                run.finish(conn, "COMPLETED", rows_written=total)
                conn.commit()
            except Exception as exc:                               # data is committed; close the run cleanly
                conn.rollback()
                run.finish(conn, "FAILED", rows_written=total, error=f"validation: {type(exc).__name__}: {exc}"[:2000])
                conn.commit()
                set_workspace_status(settings, code, "NEEDS_ATTENTION", last_committed_through=committed)
                raise
            seconds = time.perf_counter() - started
    finally:
        engine.dispose()
        control.dispose()

    set_workspace_status(settings, code, "READY", last_committed_through=committed)
    status = record_validation_outcome(settings, code, report.passed)
    log.info("master data generated", extra={"event": "generate.master_data", "workspace": code,
                                             "rows": total, "seconds": round(seconds, 2)})
    return MasterDataResult(run_number, setup_day.isoformat(), rows, seconds, report, relative, status)
