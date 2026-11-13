"""Detection report (1G §9.2, §9.4; master prompt §15E; D91): which injected anomalies the checks caught.

Steps, all on the simulation side:

1. **Loader** — an incremental load of the staging copies into ``<code>_sim.duckdb`` (if that replica exists):
   reconciliation failures become ``LD-RECON:<table>``, schema drift ``LD-SCHEMA_DRIFT:<table>``.
2. **CDC consumer** — applies new change events (if any): rejected events become ``CDC-DUPLICATE:<table>`` /
   ``CDC-STALE:<table>``, a target that didn't converge to the source ``CDC-CONVERGENCE:<table>``.
3. **Validation suite** — every ``V-*`` rule on ``scm_<code>_sim`` (the same code as on clean data).
4. **Data-quality checks** — ``DQ-*`` (``simulation.dq``).

Each registered anomaly is then **caught** (all its expected checks failed), **partly caught** or **missed**; a
failing check that no anomaly expects (``expect``) or accepts (``may_fail``) is **unexplained** (a false alarm, or an anomaly with an incomplete
expectation), except rules already failing on the clean data (from the workspace's last validation report),
which are listed as **pre-existing**. The report is written to ``data/reports/<code>/sim_detection_*.json``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import text

from scmgen.core.calendar import wall_clock
from scmgen.core.settings import Settings
from scmgen.db.workspaces import get_workspace
from scmgen.simulation import sim_schema
from scmgen.simulation.dq import run_dq


@dataclass
class AnomalyOutcome:
    anomaly_id: int
    scenario: str
    anomaly_type: str
    table: str
    target_pk: int | None
    expected: list[str]
    failed: list[str]
    status: str                       # CAUGHT / PARTLY_CAUGHT / MISSED / NO_EXPECTATION


@dataclass
class DetectionReport:
    workspace: str
    created_at: str
    failing_checks: dict[str, int]                        # check id → violations
    anomalies: list[AnomalyOutcome] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)
    pre_existing: list[str] = field(default_factory=list)
    steps: dict[str, str] = field(default_factory=dict)
    path: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.anomalies:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts


def _clean_failures(settings: Settings, code: str) -> set[str]:
    folder = settings.data_dir / "reports" / code
    reports = sorted((p for p in folder.glob("*.json") if not p.name.startswith("sim_")),
                     key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    for path in reports:
        try:
            body = json.loads(path.read_text())
            return {r["rule_id"] for r in body["results"] if r["status"] in ("FAILED", "ERROR")}
        except (OSError, ValueError, KeyError):
            continue
    return set()


def detect(settings: Settings, code: str, run_load: bool = True, run_cdc: bool = True) -> DetectionReport:
    from scmgen.loader.run import load_workspace, replica_path
    from scmgen.simulation import cdc
    from scmgen.validation.catalogue import load_catalogue
    from scmgen.validation.framework import ValidationContext, run_checks

    workspace = get_workspace(settings, code)
    failing: dict[str, int] = {}
    steps: dict[str, str] = {}

    clock = time.perf_counter()
    if run_load and replica_path(workspace, "sim").exists() and _has_sim_replica(workspace):
        result = load_workspace(settings, code, source="sim")
        for table in result.tables:
            if table.reconciliation == "FAILED":
                failing[f"LD-RECON:{table.table}"] = 1
            if table.message and table.message.startswith("schema drift"):
                failing[f"LD-SCHEMA_DRIFT:{table.table}"] = 1
        steps["loader"] = f"{result.status}, {sum(t.rows_extracted for t in result.tables)} rows, " \
                          f"{time.perf_counter() - clock:.1f} s"
    else:
        steps["loader"] = "skipped (no sim replica: `scmgen load -w CODE --source sim`)"

    engine = sim_schema.sim_engine(settings, workspace)
    try:
        with engine.connect() as conn:
            if not sim_schema.info(conn).get("sim_build_id"):
                raise sim_schema_error(code)
            has_events = conn.execute(text("SELECT COUNT(*) FROM change_event_log")).scalar()
            registry = [dict(r) for r in conn.execute(text("""
                SELECT r.anomaly_id, s.scenario_name, r.anomaly_type, r.target_table, r.target_pk,
                       r.expected_failing_checks, r.collateral_checks
                FROM anomaly_registry r JOIN anomaly_scenarios s USING (anomaly_scenario_id)
                ORDER BY r.anomaly_id""")).mappings()]

            clock = time.perf_counter()
            if run_cdc and has_events:
                applied = cdc.apply(settings, code)
                # Anomalous events a reset has deleted since are history: count only those still in the log.
                live = {int(i) for (i,) in conn.execute(text(
                    "SELECT change_event_id FROM change_event_log WHERE is_simulated_anomaly"))}
                for lsn, table, reason, is_anomaly in cdc.rejected_rows(settings, code):
                    if not is_anomaly or lsn in live:
                        key = f"CDC-{reason}:{table}"
                        failing[key] = failing.get(key, 0) + 1
                for table, state in cdc.compare(settings, code).items():
                    if state.startswith("FAILED"):
                        failing[f"CDC-CONVERGENCE:{table}"] = 1
                steps["cdc"] = f"{applied.applied} events applied, rejected {applied.rejected or 0}, " \
                               f"{time.perf_counter() - clock:.1f} s"
            else:
                steps["cdc"] = "skipped (no change events)"

            clock = time.perf_counter()
            report = run_checks(ValidationContext(conn), load_catalogue())
            for result in report.results:
                if result.status in ("FAILED", "ERROR"):
                    failing[result.rule_id] = result.violations or 1
            steps["validation"] = f"{len(report.results)} rules, {time.perf_counter() - clock:.1f} s"

            clock = time.perf_counter()
            dq = run_dq(conn, workspace.oltp_schema, workspace.sim_schema)
            for result in dq:
                failing[result.check_id] = result.violations
            steps["data_quality"] = f"{len(dq)} failing, {time.perf_counter() - clock:.1f} s"
    finally:
        engine.dispose()

    clean = _clean_failures(settings, code)
    outcome = DetectionReport(code, wall_clock().isoformat(timespec="seconds"), dict(sorted(failing.items())),
                              steps=steps)
    expected_anywhere: set[str] = set()
    for row in registry:
        expected = _json_list(row["expected_failing_checks"])
        expected_anywhere.update(expected)
        expected_anywhere.update(_json_list(row["collateral_checks"]))
        failed = [c for c in expected if c in failing]
        status = ("NO_EXPECTATION" if not expected else "CAUGHT" if len(failed) == len(expected) else
                  "PARTLY_CAUGHT" if failed else "MISSED")
        outcome.anomalies.append(AnomalyOutcome(int(row["anomaly_id"]), row["scenario_name"], row["anomaly_type"],
                                                row["target_table"], row["target_pk"], expected, failed, status))
    outcome.pre_existing = sorted(c for c in failing if c in clean)
    outcome.unexplained = sorted(c for c in failing if c not in expected_anywhere and c not in clean)
    folder = settings.data_dir / "reports" / code
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"sim_detection_{wall_clock():%Y%m%d_%H%M%S}.json"
    body = asdict(outcome)
    body["counts"] = outcome.counts
    path.write_text(json.dumps(body, indent=2, default=str))
    outcome.path = str(path)
    return outcome


def _json_list(value) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else json.loads(value)


def sim_schema_error(code: str) -> Exception:
    from scmgen.simulation.anomalies import AnomalyError
    return AnomalyError(f"No staging copies yet: run `scmgen simulate reset -w {code}` first")


def _has_sim_replica(workspace) -> bool:
    import duckdb

    from scmgen.loader.run import replica_path
    con = duckdb.connect(str(replica_path(workspace, "sim")), read_only=True)
    try:
        return bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'etl_meta' "
                                "AND table_name = 'replica_info'").fetchone()[0])
    finally:
        con.close()


def latest_report(settings: Settings, code: str) -> dict | None:
    folder = settings.data_dir / "reports" / code
    reports = sorted(folder.glob("sim_detection_*.json")) if folder.exists() else []
    return json.loads(Path(reports[-1]).read_text()) if reports else None
