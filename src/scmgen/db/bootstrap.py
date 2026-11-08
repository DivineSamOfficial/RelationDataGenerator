"""`scmgen db init`: create service accounts, grant privileges, create the control schema.

Idempotent: safe to run any number of times. Each run
- generates a password for any service account that has none in .env (and writes it there),
- creates missing accounts and re-applies the password from .env to existing ones,
- re-applies grants (GRANT is additive and idempotent),
- creates scm_control and its tables if missing.

Privileges (Part 1A D8, Part 1G §4, D73):
  app     ALL PRIVILEGES      on `scm\\_%`
  reader  SELECT, SHOW VIEW   on `scm\\_%`
  anomaly SELECT, SHOW VIEW   on `scm\\_%\\_oltp` and `scm\\_control`; ALL PRIVILEGES on `scm\\_%\\_sim`
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, Engine, text

from scmgen.core.env_file import generate_password, upsert_env_value
from scmgen.core.errors import ConfigError, ServerRequirementError
from scmgen.core.settings import PASSWORD_ENV_VARS, SERVICE_ROLES, Role, Settings
from scmgen.db.connection import create_mysql_engine, verify_connection
from scmgen.db.server_check import evaluate_server, fetch_server_info
from scmgen.db.sql_files import execute_raw, read_sql, run_sql

log = logging.getLogger(__name__)

_USER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HOST_RE = re.compile(r"^(localhost|[0-9.]+|%)$")
_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

CONTROL_DDL_FILES = ("ddl/control/0001_workspaces.sql",)


@dataclass
class InitReport:
    actions: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.actions.append(message)
        log.info(message, extra={"event": "db.init"})


def like_escape(name: str) -> str:
    """Escape '_' and '%' so a literal name can be used inside a GRANT database pattern."""
    return name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


def grant_statements(settings: Settings) -> list[tuple[Role, str]]:
    """The full privilege plan as SQL, one statement per (role, grant)."""
    prefix = settings.schemas.workspace_prefix
    control = settings.schemas.control_schema
    for name in (prefix.rstrip("_"), control):
        if not _SCHEMA_RE.match(name):
            raise ConfigError(f"Unsafe schema name or prefix in config: {name!r}")

    every_schema = like_escape(prefix) + "%"                    # scm\_%
    oltp_schemas = like_escape(prefix) + "%" + like_escape("_oltp")   # scm\_%\_oltp
    sim_schemas = like_escape(prefix) + "%" + like_escape("_sim")     # scm\_%\_sim
    control_schema = like_escape(control)                       # scm\_control

    def account(role: Role) -> str:
        return f"'{settings.users.name_for(role)}'@'{settings.users.host}'"

    return [
        ("app", f"GRANT ALL PRIVILEGES ON `{every_schema}`.* TO {account('app')}"),
        ("reader", f"GRANT SELECT, SHOW VIEW ON `{every_schema}`.* TO {account('reader')}"),
        ("anomaly", f"GRANT SELECT, SHOW VIEW ON `{oltp_schemas}`.* TO {account('anomaly')}"),
        ("anomaly", f"GRANT SELECT, SHOW VIEW ON `{control_schema}`.* TO {account('anomaly')}"),
        ("anomaly", f"GRANT ALL PRIVILEGES ON `{sim_schemas}`.* TO {account('anomaly')}"),
    ]


def init_database(settings: Settings, env_file: Path) -> InitReport:
    report = InitReport()
    _validate_account_names(settings)

    admin = create_mysql_engine(settings, role="admin")
    try:
        verify_connection(admin)
        _require_server(settings, admin)
        report.add("server requirements: all blocking checks passed")

        passwords = _ensure_passwords(settings, env_file, report)

        with admin.connect() as conn:
            for role in SERVICE_ROLES:
                _ensure_user(conn, settings.users.name_for(role), settings.users.host, passwords[role], report)
            for _, statement in grant_statements(settings):
                execute_raw(conn, statement)  # GRANT patterns contain '%'
            report.add(f"grants applied ({len(grant_statements(settings))} statements)")
            _ensure_control_schema(conn, settings, report)
            conn.commit()
    finally:
        admin.dispose()
    return report


def _validate_account_names(settings: Settings) -> None:
    if not _HOST_RE.match(settings.users.host):
        raise ConfigError(f"Unsafe users.host in config: {settings.users.host!r}")
    for role in SERVICE_ROLES:
        name = settings.users.name_for(role)
        if not _USER_RE.match(name):
            raise ConfigError(f"Unsafe MySQL user name for {role}: {name!r}")


def _require_server(settings: Settings, engine: Engine) -> None:
    mysql = settings.mysql
    results = evaluate_server(
        fetch_server_info(engine), mysql.min_server_version, mysql.charset, mysql.collation, mysql.session_time_zone
    )
    failures = [f"{r.name}: {r.detail}" for r in results if r.blocking and not r.passed]
    if failures:
        raise ServerRequirementError("MySQL server does not meet requirements: " + "; ".join(failures))


def _ensure_passwords(settings: Settings, env_file: Path, report: InitReport) -> dict[Role, str]:
    """Existing passwords come from settings (.env / environment); missing ones are generated
    and written to .env *before* any account is touched, so a crash can never leave an
    account whose password isn't recorded anywhere."""
    passwords: dict[Role, str] = {}
    for role in SERVICE_ROLES:
        existing = settings.credentials.get(role)
        if existing is not None:
            passwords[role] = existing.password
            continue
        password = generate_password()
        upsert_env_value(env_file, PASSWORD_ENV_VARS[role], password)
        passwords[role] = password
        report.add(f"generated password for {settings.users.name_for(role)} and saved it to {env_file.name}")
    return passwords


def _ensure_user(conn: Connection, user: str, host: str, password: str, report: InitReport) -> None:
    account = f"'{user}'@'{host}'"
    existed = conn.execute(
        text("SELECT COUNT(*) FROM mysql.user WHERE user = :u AND host = :h"), {"u": user, "h": host}
    ).scalar_one()
    if existed:
        # Re-apply the recorded password so MySQL and .env can't drift apart.
        conn.execute(text(f"ALTER USER {account} IDENTIFIED BY :pw"), {"pw": password})
        report.add(f"user {account} exists: password re-applied from .env")
    else:
        conn.execute(text(f"CREATE USER {account} IDENTIFIED BY :pw"), {"pw": password})
        report.add(f"user {account} created")


def _ensure_control_schema(conn: Connection, settings: Settings, report: InitReport) -> None:
    schema = settings.schemas.control_schema
    mysql = settings.mysql
    execute_raw(conn, f"CREATE DATABASE IF NOT EXISTS `{schema}` CHARACTER SET {mysql.charset} COLLATE {mysql.collation}")
    execute_raw(conn, f"USE `{schema}`")
    for path in CONTROL_DDL_FILES:
        run_sql(conn, read_sql(path))
    report.add(f"control schema `{schema}` ready ({len(CONTROL_DDL_FILES)} DDL file(s) applied)")
