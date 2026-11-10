"""Run bookkeeping: ``generation_runs`` and ``generation_run_steps`` (1G §5.2-5.3).

Every generation (historical, Continue) is one run row with its configuration snapshot, its simulated
window and its status; its phases are step rows the control panel polls for progress. Timing columns
and audit columns of these system tables use the wall clock (D74).

Transaction boundaries belong to the engine. The recorder only executes statements, with one rule:
``day_committed()`` must run inside the day's transaction, so ``committed_through`` (the loader's upper
bound, D67) moves forward atomically with the day's data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen import __version__
from scmgen.core.calendar import SimDay, wall_clock
from scmgen.generation.config import GenerationConfig

STEP_NAMES = ("SCHEMA", "SEED_DATA", "MASTER_DATA", "OPENING_BALANCES", "DAILY_SIMULATION", "VALIDATION", "FINALISE")
RUN_MODES = ("HISTORICAL", "CONTINUE")
TRIGGERS = ("CLI", "UI")
FINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")


class RunError(RuntimeError):
    """A run can't move to the requested state."""


@dataclass(frozen=True)
class RunWindow:
    start: date
    end: date


class RunRecorder:
    def __init__(self, run_id: int, window: RunWindow) -> None:
        self.run_id = run_id
        self.window = window

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(cls, conn: Connection, *, build_id: str, run_mode: str, triggered_by: str, base_seed: int,
               config: GenerationConfig, sim_start: date, sim_end: date) -> RunRecorder:
        """Insert a PENDING run (next run number) with its seven PENDING steps."""
        if run_mode not in RUN_MODES:
            raise RunError(f"run_mode must be one of {RUN_MODES}")
        if triggered_by not in TRIGGERS:
            raise RunError(f"triggered_by must be one of {TRIGGERS}")
        running = conn.execute(text("SELECT run_number FROM generation_runs WHERE run_status = 'RUNNING'")).first()
        if running:
            raise RunError(f"Run {running[0]} is still RUNNING; only one run may write at a time")
        now = wall_clock()
        run_number = conn.execute(text("SELECT COALESCE(MAX(run_number), 0) + 1 FROM generation_runs")).scalar_one()
        result = conn.execute(text(
            "INSERT INTO generation_runs (run_number, build_id, run_mode, triggered_by, base_seed, config_snapshot, "
            "config_hash, app_version, sim_start_date, sim_end_date, run_status, created_at, updated_at) "
            "VALUES (:n, :b, :m, :t, :s, :snap, :h, :v, :start, :end, 'PENDING', :now, :now)"),
            {"n": run_number, "b": build_id, "m": run_mode, "t": triggered_by, "s": base_seed,
             "snap": json.dumps(config.snapshot(), sort_keys=True), "h": config.config_hash(), "v": __version__,
             "start": sim_start, "end": sim_end, "now": now})
        run_id = int(result.lastrowid)
        conn.execute(text(
            "INSERT INTO generation_run_steps (generation_run_id, step_name, step_order, created_at, updated_at) "
            "VALUES (:r, :name, :order, :now, :now)"),
            [{"r": run_id, "name": name, "order": order, "now": now} for order, name in enumerate(STEP_NAMES, 1)])
        return cls(run_id, RunWindow(sim_start, sim_end))

    # ------------------------------------------------------------------ run state

    def start(self, conn: Connection) -> None:
        self._set_run(conn, "run_status = 'RUNNING', started_at = :now, process_id = :pid",
                      {"pid": os.getpid()}, expect="PENDING")

    def finish(self, conn: Connection, status: str, rows_written: int, error: str | None = None) -> None:
        if status not in FINAL_STATUSES:
            raise RunError(f"Final status must be one of {FINAL_STATUSES}")
        if status == "FAILED" and not error:
            raise RunError("A FAILED run needs an error message")
        self._set_run(conn, "run_status = :st, finished_at = :now, rows_written = :rows, error_message = :err",
                      {"st": status, "rows": rows_written, "err": error}, expect="RUNNING")

    def day_committed(self, conn: Connection, day: date) -> datetime:
        """Advance ``committed_through`` to the end of ``day``. Call inside the day's transaction."""
        if not self.window.start <= day <= self.window.end:
            raise RunError(f"{day} is outside the run window {self.window.start}..{self.window.end}")
        through = SimDay(day).end
        self._set_run(conn, "committed_through = :through", {"through": through}, expect="RUNNING")
        return through

    def cancel_requested(self, conn: Connection) -> bool:
        return bool(conn.execute(text("SELECT cancel_requested FROM generation_runs WHERE generation_run_id = :r"),
                                 {"r": self.run_id}).scalar_one())

    def _set_run(self, conn: Connection, assignments: str, params: Mapping[str, object], expect: str) -> None:
        result = conn.execute(text(
            f"UPDATE generation_runs SET {assignments}, updated_at = :now "
            f"WHERE generation_run_id = :r AND run_status = :expect"),
            {**params, "now": wall_clock(), "r": self.run_id, "expect": expect})
        if result.rowcount != 1:
            raise RunError(f"Run {self.run_id} is not {expect}")

    # ------------------------------------------------------------------ steps

    def step_start(self, conn: Connection, name: str, planned_units: int | None = None,
                   planned_rows: int | None = None) -> None:
        self._set_step(conn, name, "step_status = 'RUNNING', started_at = :now, planned_units = :pu, planned_rows = :pr",
                       {"pu": planned_units, "pr": planned_rows}, expect="PENDING")

    def step_progress(self, conn: Connection, name: str, *, completed_units: int, generated_rows: int,
                      current_sim_date: date | None = None, rows_by_table: Mapping[str, int] | None = None,
                      rows_per_second: Decimal | None = None) -> None:
        self._set_step(conn, name,
                       "completed_units = :cu, generated_rows = :gr, current_sim_date = :d, "
                       "rows_by_table = :rbt, rows_per_second = :rps",
                       {"cu": completed_units, "gr": generated_rows, "d": current_sim_date,
                        "rbt": json.dumps(dict(sorted(rows_by_table.items()))) if rows_by_table is not None else None,
                        "rps": rows_per_second}, expect="RUNNING")

    def step_finish(self, conn: Connection, name: str, status: str = "COMPLETED") -> None:
        if status not in ("COMPLETED", "FAILED"):
            raise RunError("A step finishes COMPLETED or FAILED")
        self._set_step(conn, name, "step_status = :st, finished_at = :now", {"st": status}, expect="RUNNING")

    def step_skip(self, conn: Connection, name: str) -> None:
        self._set_step(conn, name, "step_status = 'SKIPPED'", {}, expect="PENDING")

    def _set_step(self, conn: Connection, name: str, assignments: str, params: Mapping[str, object],
                  expect: str) -> None:
        if name not in STEP_NAMES:
            raise RunError(f"Unknown step {name!r}")
        result = conn.execute(text(
            f"UPDATE generation_run_steps SET {assignments}, updated_at = :now "
            f"WHERE generation_run_id = :r AND step_name = :name AND step_status = :expect"),
            {**params, "now": wall_clock(), "r": self.run_id, "name": name, "expect": expect})
        if result.rowcount != 1:
            raise RunError(f"Step {name} of run {self.run_id} is not {expect}")
