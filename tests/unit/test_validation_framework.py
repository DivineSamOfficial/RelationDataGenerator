"""Phase 3 step 3.4: rule catalogue from the design docs and the check runner (no database needed)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scmgen.core.errors import ConfigError
from scmgen.validation import framework
from scmgen.validation.catalogue import DEFAULT_DOCS_DIR, load_catalogue
from scmgen.validation.framework import ValidationError, run_checks


def test_catalogue_holds_all_101_design_rules():
    catalogue = load_catalogue()
    assert len(catalogue) == 101
    assert sum(rule.reconciliation for rule in catalogue.values()) == 14       # ★ in the design
    assert catalogue["V-IN-01"].reconciliation and not catalogue["V-MD-02"].reconciliation
    assert catalogue["V-MD-02"].module == "MD" and "check digit" in catalogue["V-MD-02"].text


def test_every_registered_check_belongs_to_a_design_rule():
    assert set(framework.registered_checks()) <= set(load_catalogue())


def test_duplicate_rule_definitions_are_refused(tmp_path: Path):
    for path in DEFAULT_DOCS_DIR.glob("phase1_part1*.md"):
        shutil.copy(path, tmp_path / path.name)
    target = tmp_path / "phase1_part1b_master_data_and_accounting_master.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n| V-MD-02 | duplicate |\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="V-MD-02 is defined twice"):
        load_catalogue(tmp_path)


class _FakeContext:
    def __init__(self, has_master_data: bool) -> None:
        self.has_master_data = has_master_data
        self.workspace_code = "demo"

        class _Conn:
            def rollback(self) -> None:
                pass
        self.conn = _Conn()


@pytest.fixture
def registry(monkeypatch):
    checks = {
        "V-MD-02": framework.RegisteredCheck("V-MD-02", lambda ctx: [], True),
        "V-MD-03": framework.RegisteredCheck("V-MD-03", lambda ctx: [{"id": i} for i in range(30)], True),
        "V-SY-01": framework.RegisteredCheck("V-SY-01", lambda ctx: 1 / 0, False),
    }
    monkeypatch.setattr(framework, "_REGISTRY", checks)
    monkeypatch.setattr(framework, "registered_checks", lambda: dict(checks))


def test_runner_statuses_and_report(registry):
    report = run_checks(_FakeContext(True), load_catalogue(), modules=["MD", "SY"])
    by_id = {r.rule_id: r for r in report.results}
    assert by_id["V-MD-02"].status == "PASSED"
    assert by_id["V-MD-03"].status == "FAILED" and by_id["V-MD-03"].violations == 30
    assert len(by_id["V-MD-03"].sample) == framework.SAMPLE_SIZE
    assert by_id["V-SY-01"].status == "ERROR" and "ZeroDivisionError" in by_id["V-SY-01"].message
    assert by_id["V-MD-01"].status == "PENDING" and "3.5" in by_id["V-MD-01"].message     # fake registry
    assert by_id["V-MD-06"].message.endswith("(planned: step 3.7)")
    assert by_id["V-SY-04"].message.endswith("(planned: step 3.16 (double-build test))")
    assert not report.passed
    body = json.loads(report.to_json())
    assert body["status"] == "FAILED" and body["counts"]["FAILED"] == 1 and len(body["results"]) == 26


def test_master_rules_are_not_applicable_without_master_data(registry):
    report = run_checks(_FakeContext(False), load_catalogue(), rule_ids=["V-MD-02", "V-MD-03"])
    assert {r.status for r in report.results} == {"NOT_APPLICABLE"} and report.passed


def test_selection_by_rule_and_unknown_rules(registry):
    report = run_checks(_FakeContext(True), load_catalogue(), rule_ids=["v-md-02"])
    assert [r.rule_id for r in report.results] == ["V-MD-02"]
    with pytest.raises(ValidationError, match="V-XX-99"):
        run_checks(_FakeContext(True), load_catalogue(), rule_ids=["V-XX-99"])


def test_checks_for_undefined_rules_are_refused(monkeypatch):
    checks = {"V-ZZ-01": framework.RegisteredCheck("V-ZZ-01", lambda ctx: [], False)}
    monkeypatch.setattr(framework, "registered_checks", lambda: checks)
    with pytest.raises(ValidationError, match="V-ZZ-01"):
        run_checks(_FakeContext(True), load_catalogue())


def test_registering_a_rule_twice_is_refused():
    framework.registered_checks()
    with pytest.raises(ValidationError, match="already has a check"):
        framework.check("V-MD-02")(lambda ctx: [])
