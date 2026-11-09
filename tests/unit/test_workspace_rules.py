from __future__ import annotations

from pathlib import Path

import pytest

from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import load_settings
from scmgen.db.workspaces import ddl_fingerprint, validate_code, validate_industries, workspace_names


@pytest.mark.parametrize("code", ["ab", "kaveri", "abc123", "z123456789"])
def test_valid_codes(code: str) -> None:
    assert validate_code(code) == code


@pytest.mark.parametrize("code", ["a", "Abc", "1abc", "ab_c", "ab-c", "abcdefghijk", "", "control"])
def test_invalid_or_reserved_codes(code: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_code(code)


def test_industries_are_cleaned_and_validated() -> None:
    assert validate_industries([" beauty", "electronics ", ""]) == ["beauty", "electronics"]
    with pytest.raises(WorkspaceError, match="At least one"):
        validate_industries(["", " "])
    with pytest.raises(WorkspaceError, match="Invalid industry"):
        validate_industries(["Beauty"])
    with pytest.raises(WorkspaceError, match="repeat"):
        validate_industries(["beauty", "beauty"])


def test_workspace_names_follow_d13(config_dir: Path) -> None:
    names = workspace_names(load_settings(config_dir, env_file=None, environ={}), "abc")
    assert (names.oltp_schema, names.sim_schema) == ("scm_abc_oltp", "scm_abc_sim")
    assert names.duckdb_path.name == "abc.duckdb"
    assert names.duckdb_sim_path.name == "abc_sim.duckdb"


def test_ddl_fingerprint_is_stable_and_lists_every_file() -> None:
    first, second = ddl_fingerprint(), ddl_fingerprint()
    assert first == second
    assert len(first["ddl_sha256"]) == 64
    assert first["ddl_files"][0] == "0101_reference.sql" and first["ddl_files"][-1] == "0701_system.sql"
