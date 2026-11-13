"""Background jobs for the control panel (1A §8.4): the UI never runs generation or loading in its own process;
it starts the same ``scmgen`` CLI commands as subprocesses, with their output in ``logs/ui_jobs/<id>.log``. Progress
of generation comes from ``generation_runs`` / ``generation_run_steps``, so closing the browser doesn't stop a job
and reopening it shows the live state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from scmgen.core.settings import PROJECT_ROOT

JOBS_DIR = PROJECT_ROOT / "logs" / "ui_jobs"


@dataclass
class Job:
    job_id: str
    label: str
    command: list[str]
    started: float
    pid: int
    log: str

    @property
    def running(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        try:                                      # a finished child stays a zombie until reaped
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return True

    def tail(self, lines: int = 40) -> str:
        path = Path(self.log)
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    @property
    def exit_code(self) -> int | None:
        marker = Path(self.log).with_suffix(".exit")
        return int(marker.read_text().strip()) if marker.exists() else None


def _scmgen() -> str:
    candidate = Path(sys.executable).with_name("scmgen")
    return str(candidate) if candidate.exists() else "scmgen"


def start(label: str, args: list[str]) -> Job:
    """Start ``scmgen <args>`` in the background; its exit code is written next to the log when it ends."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    log = JOBS_DIR / f"{job_id}.log"
    command = [_scmgen(), *args]
    shell = f'{" ".join(_quote(c) for c in command)} > {_quote(str(log))} 2>&1; echo $? > {_quote(str(log.with_suffix(".exit")))}'
    process = subprocess.Popen(["/bin/sh", "-c", shell], cwd=PROJECT_ROOT, start_new_session=True)
    job = Job(job_id, label, command, time.time(), process.pid, str(log))
    (JOBS_DIR / f"{job_id}.json").write_text(json.dumps(asdict(job)))
    return job


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def recent(limit: int = 15) -> list[Job]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for path in sorted(JOBS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            jobs.append(Job(**json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    return jobs
