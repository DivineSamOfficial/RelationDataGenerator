"""`scmgen` command-line entry point.

Exit codes: 0 = success, 1 = a blocking check failed, 2 = configuration or connection error.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from scmgen import __version__
from scmgen.core.errors import ScmgenError
from scmgen.core.logging_setup import setup_logging
from scmgen.core.settings import ALL_ROLES, DEFAULT_ENV_FILE, Settings, load_settings
from scmgen.db.bootstrap import init_database
from scmgen.db.connection import create_mysql_engine, verify_connection
from scmgen.db.server_check import evaluate_server, fetch_server_info

app = typer.Typer(help="Synthetic supply-chain OLTP generator.", no_args_is_help=True)
db_app = typer.Typer(help="MySQL server and schema commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")

log = logging.getLogger("scmgen.cli")


def _bootstrap() -> Settings:
    try:
        settings = load_settings()
    except ScmgenError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    setup_logging(settings.logging)
    return settings


@app.command()
def version() -> None:
    """Print the scmgen version."""
    typer.echo(__version__)


@db_app.command("check")
def db_check(
    role: Annotated[str, typer.Option("--as", help="Login to use: admin, app, reader or anomaly.")] = "admin",
) -> None:
    """Connect to MySQL and verify the server meets the design requirements."""
    if role not in ALL_ROLES:
        typer.secho(f"--as must be one of: {', '.join(ALL_ROLES)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    settings = _bootstrap()
    try:
        engine = create_mysql_engine(settings, role=role)  # type: ignore[arg-type]
        verify_connection(engine)
        info = fetch_server_info(engine)
    except ScmgenError as exc:
        log.error("database check failed", extra={"event": "db.check.error", "role": role})
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    mysql = settings.mysql
    results = evaluate_server(
        info, mysql.min_server_version, mysql.charset, mysql.collation, mysql.session_time_zone
    )
    typer.echo(f"MySQL {mysql.host}:{mysql.port} as '{engine.url.username}'")
    for result in results:
        status = "PASS" if result.passed else ("FAIL" if result.blocking else "WARN")
        colour = typer.colors.GREEN if result.passed else (typer.colors.RED if result.blocking else typer.colors.YELLOW)
        typer.secho(f"  [{status}] {result.name}: {result.detail}", fg=colour)

    blocking_failures = [r.name for r in results if r.blocking and not r.passed]
    log.info(
        "database check finished",
        extra={
            "event": "db.check",
            "role": role,
            "server_version": info.version,
            "blocking_failures": blocking_failures,
        },
    )
    engine.dispose()
    if blocking_failures:
        raise typer.Exit(1)


@db_app.command("init")
def db_init() -> None:
    """Create the service accounts, grant privileges and create the control schema.

    Uses the admin login. Safe to run repeatedly.
    """
    settings = _bootstrap()
    try:
        report = init_database(settings, DEFAULT_ENV_FILE)
    except ScmgenError as exc:
        log.error("database init failed", extra={"event": "db.init.error"})
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    # Each action was already printed by the console log handler as it happened.
    typer.secho(f"Database initialised ({len(report.actions)} actions).", fg=typer.colors.GREEN)
    typer.echo("Verify with: scmgen db check --as app")


if __name__ == "__main__":  # pragma: no cover
    app()
