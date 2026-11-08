"""Check that the MySQL server meets the design's requirements.

Split in two so the rules can be unit-tested without a database:
- fetch_server_info(): reads facts from the server
- evaluate_server(): pure function turning facts into pass/fail checks
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine, text

from scmgen.core.errors import ConfigError

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class ServerInfo:
    version: str
    sql_mode: str
    default_storage_engine: str
    character_set_server: str
    collation_server: str
    session_time_zone: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    blocking: bool  # True = the design can't work without it; False = advisory


def parse_version(version: str) -> tuple[int, int, int]:
    """'8.0.36-0ubuntu0.22.04.1' -> (8, 0, 36); '26.10.0' -> (26, 10, 0)."""
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise ConfigError(f"Unrecognised MySQL version string: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def fetch_server_info(engine: Engine) -> ServerInfo:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT VERSION(), @@GLOBAL.sql_mode, @@default_storage_engine, "
                "@@character_set_server, @@collation_server, @@SESSION.time_zone"
            )
        ).one()
    return ServerInfo(*[str(value) for value in row])


def evaluate_server(
    info: ServerInfo,
    min_version: str,
    expected_charset: str,
    expected_collation: str,
    expected_time_zone: str,
) -> list[CheckResult]:
    modes = {mode.strip().upper() for mode in info.sql_mode.split(",") if mode.strip()}
    strict = bool(modes & {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"})
    return [
        CheckResult(
            "server version",
            parse_version(info.version) >= parse_version(min_version),
            f"{info.version} (minimum {min_version}: CHECK constraints are enforced from 8.0.16)",
            blocking=True,
        ),
        CheckResult(
            "strict SQL mode",
            strict,
            "on" if strict else f"off: invalid values would be silently adjusted (sql_mode={info.sql_mode})",
            blocking=True,
        ),
        CheckResult(
            "default storage engine",
            info.default_storage_engine.upper() == "INNODB",
            info.default_storage_engine,
            blocking=True,
        ),
        CheckResult(
            "server character set",
            info.character_set_server == expected_charset,
            f"{info.character_set_server} (schemas set {expected_charset} explicitly, so advisory)",
            blocking=False,
        ),
        CheckResult(
            "server collation",
            info.collation_server == expected_collation,
            f"{info.collation_server} (schemas set {expected_collation} explicitly, so advisory)",
            blocking=False,
        ),
        CheckResult(
            "session time zone",
            info.session_time_zone == expected_time_zone,
            f"{info.session_time_zone} (expected {expected_time_zone}, D77)",
            blocking=True,
        ),
    ]
