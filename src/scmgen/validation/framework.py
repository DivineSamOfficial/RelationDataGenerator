"""Validation framework: check registry, runner and report (1H §8, D76).

A check is a function that receives a ``ValidationContext`` and returns the violating rows (a list of
dicts; empty = the rule holds). Checks register against a rule ID from the design catalogue:

    @check("V-MD-05", requires_master_data=True)
    def single_top_employee(ctx): ...

Rules without a registered check appear in the report as PENDING with the step that will add them,
so the report always shows the full 101-rule coverage. Every rule must pass on clean data (1B §7).
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import wall_clock
from scmgen.validation.catalogue import Rule

SAMPLE_SIZE = 20
STATUSES = ("PASSED", "FAILED", "ERROR", "NOT_APPLICABLE", "PENDING")

# Steps that will implement the rules not yet coded (shown as PENDING in reports).
PLANNED_STEPS = {
    "MD": "3.5", "SY": "3.6", "PR": "3.7", "IN": "3.7", "SO": "3.8", "SH": "3.9", "IV": "3.9", "PY": "3.10",
    "AR": "3.10", "RT": "3.11", "EX": "3.13", "AP": "3.13", "BK": "3.14", "TX": "3.14", "AC": "3.14", "LD": "Phase 4",
}
PLANNED_OVERRIDES = {"V-SY-04": "3.16 (double-build test)",
                     **{f"V-PR-{n:02d}": "3.7b" for n in (8, 9, 10, 11)},
                     **{f"V-PR-{n:02d}": "3.7c" for n in (12, 13, 14)}}


class ValidationError(RuntimeError):
    """The framework itself is misused (unknown rule, duplicate check)."""


CheckFunction = Callable[["ValidationContext"], list[dict[str, Any]]]


@dataclass(frozen=True)
class RegisteredCheck:
    rule_id: str
    function: CheckFunction
    requires_master_data: bool


_REGISTRY: dict[str, RegisteredCheck] = {}


def check(rule_id: str, requires_master_data: bool = False) -> Callable[[CheckFunction], CheckFunction]:
    """Register ``function`` as the check for ``rule_id``."""
    def register(function: CheckFunction) -> CheckFunction:
        if rule_id in _REGISTRY:
            raise ValidationError(f"{rule_id} already has a check ({_REGISTRY[rule_id].function.__name__})")
        _REGISTRY[rule_id] = RegisteredCheck(rule_id, function, requires_master_data)
        return function
    return register


def registered_checks() -> dict[str, RegisteredCheck]:
    import scmgen.validation.checks  # noqa: F401  (importing registers every check module)
    return dict(_REGISTRY)


@dataclass
class ValidationContext:
    """What checks may use: the workspace connection plus lazily loaded reference data."""

    conn: Connection
    workspace_code: str | None = None
    config_dir: Path | None = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(text(sql), params).mappings()]

    def scalar(self, sql: str, **params: Any) -> Any:
        return self.conn.execute(text(sql), params).scalar()

    @cached_property
    def has_master_data(self) -> bool:
        return bool(self.scalar("SELECT COUNT(*) FROM companies"))

    @cached_property
    def seed(self):
        from scmgen.core.settings import DEFAULT_CONFIG_DIR
        from scmgen.generation.seed import load_seed
        return load_seed(self.config_dir or DEFAULT_CONFIG_DIR)

    @cached_property
    def packs(self) -> dict:
        """Industry packs of this workspace, keyed by pack code (from ``business_units``)."""
        from scmgen.core.settings import DEFAULT_CONFIG_DIR
        from scmgen.generation.packs import load_packs
        codes = [row["industry_pack_code"] for row in self.rows("SELECT industry_pack_code FROM business_units")]
        return {pack.code: pack for pack in load_packs(codes, self.seed, self.config_dir or DEFAULT_CONFIG_DIR)}


@dataclass
class CheckResult:
    rule_id: str
    module: str
    rule: str
    reconciliation: bool
    status: str
    violations: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None
    duration_ms: int = 0


@dataclass
class ValidationReport:
    workspace: str | None
    schema: str | None
    started_at: datetime
    finished_at: datetime
    results: list[CheckResult]

    @property
    def counts(self) -> dict[str, int]:
        return {status: sum(r.status == status for r in self.results) for status in STATUSES}

    @property
    def passed(self) -> bool:
        """True when no check FAILED or hit an ERROR (pending and not-applicable rules don't fail a run)."""
        counts = self.counts
        return counts["FAILED"] == 0 and counts["ERROR"] == 0

    def to_json(self) -> str:
        def default(value: Any) -> Any:
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, bytes):
                return value.hex()
            raise TypeError(type(value).__name__)
        body = {"workspace": self.workspace, "schema": self.schema, "started_at": self.started_at,
                "finished_at": self.finished_at, "status": "PASSED" if self.passed else "FAILED",
                "counts": self.counts, "results": [asdict(r) for r in self.results]}
        return json.dumps(body, indent=2, default=default, ensure_ascii=False)

    def write(self, directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def _select(catalogue: Mapping[str, Rule], rule_ids: Iterable[str] | None, modules: Iterable[str] | None) -> list[Rule]:
    wanted_ids = {r.upper() for r in rule_ids or []}
    wanted_modules = {m.upper() for m in modules or []}
    unknown = wanted_ids - set(catalogue)
    if unknown:
        raise ValidationError(f"Unknown rule(s): {', '.join(sorted(unknown))}")
    return [rule for rule in catalogue.values()
            if (not wanted_ids or rule.rule_id in wanted_ids) and (not wanted_modules or rule.module in wanted_modules)]


def run_checks(ctx: ValidationContext, catalogue: Mapping[str, Rule], rule_ids: Sequence[str] | None = None,
               modules: Sequence[str] | None = None, schema: str | None = None) -> ValidationReport:
    """Run the selected rules (all by default) and return the report. Never writes to the database."""
    checks = registered_checks()
    orphans = set(checks) - set(catalogue)
    if orphans:
        raise ValidationError(f"Checks registered for rules the design doesn't define: {', '.join(sorted(orphans))}")
    started = wall_clock()
    results = []
    for rule in _select(catalogue, rule_ids, modules):
        result = CheckResult(rule.rule_id, rule.module, rule.text, rule.reconciliation, "PENDING")
        registered = checks.get(rule.rule_id)
        if registered is None:
            step = PLANNED_OVERRIDES.get(rule.rule_id) or PLANNED_STEPS.get(rule.module, "later")
            result.message = f"not implemented yet (planned: step {step})"
        elif registered.requires_master_data and not ctx.has_master_data:
            result.status, result.message = "NOT_APPLICABLE", "no master data generated yet"
        else:
            clock = time.perf_counter()
            try:
                violations = registered.function(ctx)
                result.status = "FAILED" if violations else "PASSED"
                result.violations = len(violations)
                result.sample = violations[:SAMPLE_SIZE]
            except Exception as exc:  # a broken check must show up, not stop the other checks
                result.status = "ERROR"
                result.message = f"{type(exc).__name__}: {exc}"
                result.sample = [{"traceback": traceback.format_exc(limit=3)}]
                ctx.conn.rollback()
            result.duration_ms = int((time.perf_counter() - clock) * 1000)
        results.append(result)
    return ValidationReport(ctx.workspace_code, schema, started, wall_clock(), results)
