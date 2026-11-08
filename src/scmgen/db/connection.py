"""Create SQLAlchemy engines for MySQL (PyMySQL driver).

Two roles (Part 1A D8, Part 1G §4):
- "admin": creates users, schemas and the control registry. Nothing else.
- "app":   scm_app, used for all normal work (from Phase 2 step 2.2 onwards).
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from scmgen.core.errors import DatabaseConnectionError
from scmgen.core.settings import Role, Settings

log = logging.getLogger(__name__)


def create_mysql_engine(settings: Settings, role: Role = "app", database: str | None = None) -> Engine:
    """Return an engine for the given role. No connection is opened until first use."""
    creds = settings.credentials_for(role)
    mysql = settings.mysql
    url = URL.create(
        drivername="mysql+pymysql",
        username=creds.user,
        password=creds.password or None,
        host=mysql.host,
        port=mysql.port,
        database=database,
        query={"charset": mysql.charset},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": mysql.connect_timeout_seconds,
            # Session time zone fixed to IST (D77) regardless of server settings.
            "init_command": f"SET time_zone = '{mysql.session_time_zone}'",
        },
    )


def verify_connection(engine: Engine) -> None:
    """Open one connection and run SELECT 1; raise DatabaseConnectionError with a clear message."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        url = engine.url
        raise DatabaseConnectionError(
            f"Cannot connect to MySQL at {url.host}:{url.port} as '{url.username}': {exc.orig}"
        ) from exc
    log.debug("connection ok", extra={"event": "db.connect", "user": engine.url.username})
