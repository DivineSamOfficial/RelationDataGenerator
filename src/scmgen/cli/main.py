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
from scmgen.db.design_check import run_design_check
from scmgen.db.server_check import evaluate_server, fetch_server_info
from scmgen.db.workspaces import WorkspaceInfo, create_workspace, delete_workspace, get_workspace, list_workspaces

app = typer.Typer(help="Synthetic supply-chain OLTP generator.", no_args_is_help=True)
db_app = typer.Typer(help="MySQL server and schema commands.", no_args_is_help=True)
workspace_app = typer.Typer(help="Create, list, inspect and delete company workspaces.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(workspace_app, name="workspace")

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


@db_app.command("check-design")
def db_check_design(
    workspace: Annotated[str | None, typer.Option(
        "--workspace", "-w", help="Check this workspace's live schema instead of a fresh build of the DDL.")] = None,
) -> None:
    """Compare the schema with the table definitions in docs/design/ and list every difference.

    Exit code 1 if any difference is found.
    """
    settings = _bootstrap()
    try:
        scopes = run_design_check(settings, workspace=workspace)
    except ScmgenError as exc:
        _fail(exc, "db.check_design.error")
    total = 0
    for scope in scopes:
        count = len(scope.differences)
        total += count
        colour = typer.colors.GREEN if count == 0 else typer.colors.RED
        typer.secho(f"[{'OK' if count == 0 else 'DIFF'}] {scope.summary()}: {count} difference(s)", fg=colour)
        for difference in scope.differences:
            typer.echo(f"  {difference.describe()}")
    if total:
        log.error("schema differs from design", extra={"event": "db.check_design.diff"})
        raise typer.Exit(1)
    typer.secho("Schema matches the design.", fg=typer.colors.GREEN)


# --- workspace commands -----------------------------------------------------------------------

def _fail(exc: ScmgenError, event: str) -> None:
    log.error(str(exc), extra={"event": event})
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(2) from exc


def _schema_health(tables: int | None) -> str:
    return "missing" if tables is None else f"{tables} tables"


@workspace_app.command("create")
def workspace_create(
    code: Annotated[str, typer.Option("--code", help="Short workspace code, e.g. 'kaveri' (2-10 lower-case letters/digits).")],
    company: Annotated[str, typer.Option("--company", help="Fictional company name.")],
    industries: Annotated[str, typer.Option("--industries", help="Comma-separated industry packs, e.g. 'beauty,electronics'.")],
    seed: Annotated[int | None, typer.Option("--seed", help="Base random seed (default: random, recorded).")] = None,
) -> None:
    """Register a workspace and build its MySQL schemas (empty until data generation in Phase 3)."""
    settings = _bootstrap()
    try:
        info = create_workspace(settings, code, company, industries.split(","), base_seed=seed)
    except ScmgenError as exc:
        _fail(exc, "workspace.create.error")
    typer.secho(f"Workspace '{info.code}' is {info.status}.", fg=typer.colors.GREEN)
    typer.echo(f"  clean schema       {info.oltp_schema}  ({_schema_health(info.oltp_tables)})")
    typer.echo(f"  simulation schema  {info.sim_schema}  ({_schema_health(info.sim_tables)}, filled in Phase 7)")
    typer.echo(f"  base seed          {info.base_seed}")
    typer.echo(f"  build id           {info.build_id}")


@workspace_app.command("list")
def workspace_list() -> None:
    """List registered workspaces with a live schema health check."""
    settings = _bootstrap()
    try:
        workspaces = list_workspaces(settings)
    except ScmgenError as exc:
        _fail(exc, "workspace.list.error")
    if not workspaces:
        typer.echo("No workspaces yet. Create one with: scmgen workspace create --code ... --company ... --industries ...")
        return
    typer.echo(f"{'CODE':<11}{'STATUS':<17}{'COMPANY':<34}{'CLEAN SCHEMA':<26}TABLES")
    for ws in workspaces:
        typer.echo(f"{ws.code:<11}{ws.status:<17}{ws.company_name[:32]:<34}{ws.oltp_schema:<26}{_schema_health(ws.oltp_tables)}")


@workspace_app.command("show")
def workspace_show(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
) -> None:
    """Show one workspace in detail."""
    settings = _bootstrap()
    try:
        ws: WorkspaceInfo = get_workspace(settings, code)
    except ScmgenError as exc:
        _fail(exc, "workspace.show.error")
    for label, value in [
        ("code", ws.code), ("company", ws.company_name), ("industries", ", ".join(ws.industries)),
        ("status", ws.status), ("clean schema", f"{ws.oltp_schema} ({_schema_health(ws.oltp_tables)})"),
        ("simulation schema", f"{ws.sim_schema} ({_schema_health(ws.sim_tables)})"),
        ("duckdb file", ws.duckdb_path), ("base seed", ws.base_seed), ("build id", ws.build_id),
        ("created", ws.created_at), ("data up to", ws.last_committed_through or "no data generated yet"),
    ]:
        typer.echo(f"  {label:<18}{value}")


@workspace_app.command("delete")
def workspace_delete(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    purge: Annotated[bool, typer.Option("--purge", help="Also remove the registry entry so the code can be reused.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt (scripts / tests).")] = False,
) -> None:
    """Drop a workspace's schemas and DuckDB files. Irreversible."""
    settings = _bootstrap()
    if not yes:
        typer.secho(
            f"This permanently drops all data of workspace '{code}' (MySQL schemas and DuckDB files).",
            fg=typer.colors.YELLOW,
        )
        typed = typer.prompt("Type the workspace code to confirm")
        if typed.strip() != code:
            typer.secho("Confirmation did not match. Nothing was deleted.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
    try:
        outcome = delete_workspace(settings, code, purge=purge)
    except ScmgenError as exc:
        _fail(exc, "workspace.delete.error")
    typer.secho(f"Workspace '{code}' deleted ({outcome}).", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
