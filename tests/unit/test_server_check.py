from __future__ import annotations

import pytest

from scmgen.core.errors import ConfigError
from scmgen.db.server_check import ServerInfo, evaluate_server, parse_version

GOOD = ServerInfo(
    version="8.4.3",
    sql_mode="ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_DATE",
    default_storage_engine="InnoDB",
    character_set_server="utf8mb4",
    collation_server="utf8mb4_0900_ai_ci",
    session_time_zone="+05:30",
)


def _evaluate(info: ServerInfo) -> dict[str, tuple[bool, bool]]:
    results = evaluate_server(info, "8.0.16", "utf8mb4", "utf8mb4_0900_ai_ci", "+05:30")
    return {r.name: (r.passed, r.blocking) for r in results}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8.0.16", (8, 0, 16)),
        ("8.0.36-0ubuntu0.22.04.1", (8, 0, 36)),
        ("8.4.3-log", (8, 4, 3)),
        ("26.10.0", (26, 10, 0)),
    ],
)
def test_parse_version(raw: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(raw) == expected


def test_parse_version_rejects_garbage() -> None:
    with pytest.raises(ConfigError):
        parse_version("mariadb")


def test_version_comparison_is_numeric_not_textual() -> None:
    # As text "26.10.0" < "8.0.16"; as numbers it is newer. Guards against a classic bug.
    assert parse_version("26.10.0") > parse_version("8.0.16")
    assert parse_version("8.0.9") < parse_version("8.0.16")


def test_good_server_passes_everything() -> None:
    assert all(passed for passed, _ in _evaluate(GOOD).values())


def test_old_server_fails_blocking_check() -> None:
    checks = _evaluate(ServerInfo(**{**GOOD.__dict__, "version": "8.0.15"}))
    assert checks["server version"] == (False, True)


def test_non_strict_mode_fails_blocking_check() -> None:
    checks = _evaluate(ServerInfo(**{**GOOD.__dict__, "sql_mode": "NO_ENGINE_SUBSTITUTION"}))
    assert checks["strict SQL mode"] == (False, True)


def test_other_charset_is_only_a_warning() -> None:
    checks = _evaluate(ServerInfo(**{**GOOD.__dict__, "character_set_server": "latin1"}))
    assert checks["server character set"] == (False, False)


def test_wrong_session_time_zone_fails() -> None:
    checks = _evaluate(ServerInfo(**{**GOOD.__dict__, "session_time_zone": "SYSTEM"}))
    assert checks["session time zone"] == (False, True)
