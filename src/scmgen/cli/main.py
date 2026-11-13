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
from scmgen.validation.catalogue import load_catalogue
from scmgen.validation.framework import ValidationContext, run_checks
from scmgen.generation.config import load_generation_config
from scmgen.generation.packs import available_packs, load_packs
from scmgen.generation.seed import load_seed
from scmgen.db.server_check import evaluate_server, fetch_server_info
from scmgen.db.workspaces import (
    WorkspaceInfo,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    record_validation_outcome,
)

app = typer.Typer(help="Synthetic supply-chain OLTP generator.", no_args_is_help=True)
db_app = typer.Typer(help="MySQL server and schema commands.", no_args_is_help=True)
workspace_app = typer.Typer(help="Create, list, inspect and delete company workspaces.", no_args_is_help=True)
generate_app = typer.Typer(help="Data generation (Phase 3).", no_args_is_help=True)
report_app = typer.Typer(help="Reports over a generated workspace.", no_args_is_help=True)
load_app = typer.Typer(help="Load the DuckDB analytics replica (Phase 4).")
app.add_typer(db_app, name="db")
app.add_typer(workspace_app, name="workspace")
app.add_typer(generate_app, name="generate")
app.add_typer(report_app, name="report")
app.add_typer(load_app, name="load")

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


# --- validate ---------------------------------------------------------------------------------

@app.command("validate")
def validate(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    checks: Annotated[list[str], typer.Option("--check", help="Run only this rule, e.g. V-MD-02 (repeatable).")] = [],
    modules: Annotated[list[str], typer.Option("--module", help="Run only this module, e.g. MD (repeatable).")] = [],
    show_all: Annotated[bool, typer.Option("--all", help="Also list passed, pending and not-applicable rules.")] = False,
) -> None:
    """Check a workspace's clean data against the design's validation rules and write a JSON report.

    A failure marks the workspace NEEDS_ATTENTION, which blocks Continue and loading until it passes (D76).
    Exit code 1 if any rule fails.
    """
    settings = _bootstrap()
    try:
        ws = get_workspace(settings, code)
        engine = create_mysql_engine(settings, role="app", database=ws.oltp_schema)
        try:
            with engine.connect() as conn:
                report = run_checks(ValidationContext(conn, workspace_code=ws.code), load_catalogue(),
                                    rule_ids=checks or None, modules=modules or None, schema=ws.oltp_schema)
                conn.rollback()
        finally:
            engine.dispose()
        path = report.write(settings.data_dir / "reports" / ws.code,
                            f"validation_{report.started_at:%Y%m%d_%H%M%S}")
        status = record_validation_outcome(settings, ws.code, report.passed)
    except ScmgenError as exc:
        _fail(exc, "validate.error")
    for result in report.results:
        if show_all or result.status in ("FAILED", "ERROR"):
            colour = {"PASSED": typer.colors.GREEN, "FAILED": typer.colors.RED, "ERROR": typer.colors.RED}.get(
                result.status, typer.colors.YELLOW)
            detail = f"{result.violations} violation(s)" if result.status == "FAILED" else (result.message or "")
            typer.secho(f"  [{result.status:<14}] {result.rule_id}  {detail}", fg=colour)
            if result.status in ("FAILED", "ERROR"):
                typer.echo(f"      {result.rule[:110]}")
                for row in result.sample[:3]:
                    typer.echo(f"      e.g. {row}")
    counts = report.counts
    summary = ", ".join(f"{counts[s]} {s.lower().replace('_', ' ')}" for s in counts if counts[s])
    colour = typer.colors.GREEN if report.passed else typer.colors.RED
    typer.secho(f"Validation {'PASSED' if report.passed else 'FAILED'}: {summary}", fg=colour)
    typer.echo(f"Report: {path}")
    typer.echo(f"Workspace status: {status}")
    if not report.passed:
        raise typer.Exit(1)


# --- generate commands ------------------------------------------------------------------------

def _parse_sets(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            typer.secho(f"--set expects key=value, got {item!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        overrides[key.strip()] = value.strip()
    return overrides


@generate_app.command("config")
def generate_config(
    preset: Annotated[str | None, typer.Option("--preset", help="small, medium or large (default from config).")] = None,
    sets: Annotated[list[str], typer.Option(
        "--set", help="Override a setting, e.g. --set volumes.customers_b2b=500 (repeatable).")] = [],
) -> None:
    """Show the effective generation settings (preset + overrides) and their hash."""
    _bootstrap()
    try:
        config = load_generation_config(preset=preset, overrides=_parse_sets(sets))
    except ScmgenError as exc:
        _fail(exc, "generate.config.error")
    typer.echo(f"preset            {config.preset}")
    typer.echo(f"simulated days    {config.start_date} .. {config.end_date} ({config.days} days)")
    typer.echo(f"financial year    starts in month {config.fiscal_year_start_month}")
    typer.echo(f"batch size        {config.batch_size} rows per INSERT")
    typer.echo("volumes")
    for name, value in vars(config.volumes).items():
        typer.echo(f"  {name:<22}{value:>10,}")
    environment = config.snapshot()["environment"]
    typer.echo(f"environment       app {environment['app_version']}, Python {environment['python']}, "
               f"Faker {environment['faker']}")
    typer.echo(f"config hash       {config.config_hash()}")


@generate_app.command("master-data")
def generate_master_data_command(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code (must be READY and empty).")],
    preset: Annotated[str | None, typer.Option("--preset", help="small, medium or large (default from config).")] = None,
    sets: Annotated[list[str], typer.Option("--set", help="Override a setting (repeatable).")] = [],
) -> None:
    """Generate the master data of an empty workspace on day 0 (the day before the first simulated day)."""
    from scmgen.generation.master import generate_master_data

    settings = _bootstrap()
    try:
        config = load_generation_config(preset=preset, overrides=_parse_sets(sets))
        result = generate_master_data(settings, code, config)
    except ScmgenError as exc:
        _fail(exc, "generate.master_data.error")
    typer.echo(f"Run {result.run_number}: master data on setup day {result.setup_day} "
               f"({sum(result.rows_by_table.values()):,} rows in {result.seconds:.1f}s)")
    for table, rows in sorted(result.rows_by_table.items()):
        typer.echo(f"  {table:<28}{rows:>8,}")
    counts = result.report.counts
    colour = typer.colors.GREEN if result.report.passed else typer.colors.RED
    typer.secho(f"Validation {'PASSED' if result.report.passed else 'FAILED'}: {counts['PASSED']} passed, "
                f"{counts['FAILED']} failed, {counts['ERROR']} errors, {counts['PENDING']} pending "
                f"-> {result.report_path}", fg=colour)
    for check_result in result.report.results:
        if check_result.status in ("FAILED", "ERROR"):
            typer.echo(f"  {check_result.rule_id}: {check_result.message or ''} {check_result.sample[:2]}")
    typer.echo(f"Workspace status: {result.workspace_status}")
    if not result.report.passed:
        raise typer.Exit(1)


@generate_app.command("history")
def generate_history_command(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    preset: Annotated[str | None, typer.Option("--preset", help="small, medium or large (default from config).")] = None,
    days: Annotated[int | None, typer.Option("--days", help="Simulate only this many days.")] = None,
    sets: Annotated[list[str], typer.Option("--set", help="Override a setting (repeatable).")] = [],
    scenario: Annotated[str | None, typer.Option(
        "--scenario", help="A scenario profile of config/scenarios.yaml, e.g. stress (applied before --set).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Run although the last validation failed.")] = False,
) -> None:
    """Generate master data (if missing) and simulate day by day up to the configured end date."""
    from scmgen.generation.engine import generate_history
    from scmgen.generation.scenarios import profile_overrides

    settings = _bootstrap()
    try:
        overrides = {**profile_overrides(scenario), **_parse_sets(sets)}
        config = load_generation_config(preset=preset, overrides=overrides)
        result = generate_history(settings, code, config, days=days, force=force)
    except ScmgenError as exc:
        _fail(exc, "generate.history.error")
    _print_history(result)


def _print_history(result) -> None:
    if result.master_data_rows:
        typer.echo(f"Master data: {result.master_data_rows:,} rows on day 0")
    if result.status == "NOTHING_TO_DO":
        typer.echo("Nothing to simulate: the workspace is already up to the end date.")
        return
    typer.echo(f"Run {result.run_number}: {result.status}, {result.days} day(s) "
               f"{result.first_day} .. {result.last_day} in {result.seconds:.1f}s")
    for table, rows in sorted(result.rows_by_table.items()):
        typer.echo(f"  {table:<28}{rows:>8,}")
    counts = result.report.counts
    colour = typer.colors.GREEN if result.report.passed else typer.colors.RED
    typer.secho(f"Validation {'PASSED' if result.report.passed else 'FAILED'}: {counts['PASSED']} passed, "
                f"{counts['FAILED']} failed, {counts['ERROR']} errors, {counts['PENDING']} pending "
                f"-> {result.report_path}", fg=colour)
    for check_result in result.report.results:
        if check_result.status in ("FAILED", "ERROR"):
            typer.echo(f"  {check_result.rule_id}: {check_result.message or ''} {check_result.sample[:2]}")
    typer.echo(f"Workspace status: {result.workspace_status}")
    if not result.report.passed:
        raise typer.Exit(1)


@generate_app.command("packs")
def generate_packs(
    industries: Annotated[str | None, typer.Option(
        "--industries", help="Comma-separated packs to check together (default: every shipped pack).")] = None,
) -> None:
    """Check the seed data and industry packs, and summarise each pack."""
    _bootstrap()
    codes = [c.strip() for c in industries.split(",")] if industries else available_packs()
    try:
        seed = load_seed()
        packs = load_packs(codes, seed)
    except ScmgenError as exc:
        _fail(exc, "generate.packs.error")
    typer.echo(f"Seed data OK: {len(seed.states)} states, {len(seed.cities)} cities, {len(seed.accounts)} accounts, "
               f"{len(seed.expense_categories)} expense categories")
    width = max(len(p.code) for p in packs) + 2
    typer.echo(f"{'PACK':<{width}}{'BUSINESS UNIT':<30}{'CATEGORIES':>11}{'PRODUCT TYPES':>15}{'BRANDS':>8}{'EVENTS':>8}{'B2B SALES':>11}")
    for pack in packs:
        typer.echo(f"{pack.code:<{width}}{pack.business_unit_code + ' ' + pack.business_unit_name:<30}"
                   f"{len(pack.categories):>11}{len(pack.leaves):>15}{len(pack.brands):>8}{len(pack.events):>8}"
                   f"{pack.b2b_sales_share:>10.0%}")
    typer.secho("Packs OK.", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()


@report_app.command("scenarios")
def report_scenarios(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
) -> None:
    """How often each master-prompt business scenario occurs in the workspace, against config/scenarios.yaml."""
    from scmgen.db.connection import create_mysql_engine
    from scmgen.db.workspaces import get_workspace
    from scmgen.generation.scenarios import load_scenarios
    from scmgen.reports.scenarios import measure

    settings = _bootstrap()
    try:
        workspace = get_workspace(settings, code)
        scenarios = load_scenarios()["scenarios"]
        engine = create_mysql_engine(settings, role="app", database=workspace.oltp_schema)
        with engine.connect() as conn:
            rows = measure(conn, scenarios)
        engine.dispose()
    except ScmgenError as exc:
        _fail(exc, "report.scenarios.error")
    typer.echo(f"{'Scenario':<38}{'Measured':>12}  {'Band':<18}{'Status':<8}Count")
    off = 0
    for row in rows:
        shown = "-" if row.value is None else (f"{row.value:.1f}/month" if row.unit == "per_month" else f"{row.value:.1%}")
        band = (f"{row.band[0]:g}-{row.band[1]:g}/month" if row.unit == "per_month"
                else f"{row.band[0]:.0%}-{row.band[1]:.0%}")
        colour = {"OK": typer.colors.GREEN, "NO_DATA": typer.colors.YELLOW}.get(row.status, typer.colors.RED)
        typer.echo(f"{row.scenario:<38}{shown:>12}  {band:<18}", nl=False)
        typer.secho(f"{row.status:<8}", fg=colour, nl=False)
        typer.echo(f"{row.numerator:,.0f} of {row.denominator:,.0f}")
        if row.status in ("LOW", "HIGH"):
            off += 1
            typer.echo(f"{'':<40}tune: {', '.join(row.knobs)}")
    typer.echo(f"{len(rows) - off} of {len(rows)} scenarios within their band.")


@generate_app.command("continue")
def generate_continue(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    days: Annotated[int | None, typer.Option("--days", help="Simulate this many more days (default: up to yesterday).")] = None,
    allow_future: Annotated[bool, typer.Option("--allow-future", help="Allow simulating today or later (D68).")] = False,
    force: Annotated[bool, typer.Option("--force", help="Run although the last validation failed (D76).")] = False,
) -> None:
    """Continue the workspace's history with the same settings as its last run (D14, D68)."""
    from scmgen.generation.actions import continue_workspace

    settings = _bootstrap()
    try:
        result = continue_workspace(settings, code, days=days, allow_future=allow_future, force=force)
    except ScmgenError as exc:
        _fail(exc, "generate.continue.error")
    _print_history(result)


@generate_app.command("rebuild")
def generate_rebuild(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    confirm: Annotated[str, typer.Option("--confirm", help="Type the workspace code again: every row is dropped.")],
) -> None:
    """Drop and regenerate the workspace's history with the same seed and settings (same data, D14, V-SY-04)."""
    from scmgen.generation.actions import rebuild_workspace

    settings = _bootstrap()
    try:
        result = rebuild_workspace(settings, code, confirm)
    except ScmgenError as exc:
        _fail(exc, "generate.rebuild.error")
    _print_history(result)


@generate_app.command("plan")
def generate_plan(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    days: Annotated[int | None, typer.Option("--days", help="As `continue --days`.")] = None,
    allow_future: Annotated[bool, typer.Option("--allow-future", help="As `continue --allow-future`.")] = False,
) -> None:
    """Show what `generate continue` would simulate, without writing anything."""
    from scmgen.generation.actions import plan

    settings = _bootstrap()
    try:
        result = plan(settings, code, days=days, allow_future=allow_future)
    except ScmgenError as exc:
        _fail(exc, "generate.plan.error")
    typer.echo(f"Workspace {result.code} ({result.status}), preset {result.preset}")
    if result.overrides:
        typer.echo("Overrides: " + ", ".join(f"{k}={v}" for k, v in sorted(result.overrides.items())))
    if result.days:
        typer.echo(f"Continue would simulate {result.days} day(s): {result.first_day} .. {result.last_day}")
    else:
        typer.echo("Nothing to simulate: the workspace is already up to date.")
    if result.blocked:
        typer.secho(f"Blocked: {result.blocked}", fg=typer.colors.RED)


@load_app.callback(invoke_without_command=True)
def load_command(
    ctx: typer.Context,
    code: Annotated[str | None, typer.Option("--workspace", "-w", help="Workspace code.")] = None,
    full: Annotated[bool, typer.Option("--full", help="Reload every table (or the --table ones) in full.")] = False,
    tables: Annotated[list[str], typer.Option("--table", help="Load only this table (repeatable); implies a reload.")] = [],
    force: Annotated[bool, typer.Option("--force", help="Load although the last validation failed (D76).")] = False,
    source: Annotated[str, typer.Option("--source", help="oltp (clean data) or sim (Phase 7 staging copies).")] = "oltp",
) -> None:
    """Load the workspace's MySQL data into its DuckDB replica: full, incremental or per table (1G §6)."""
    if ctx.invoked_subcommand is not None:
        return
    if not code:
        typer.secho("Missing option '--workspace' / '-w'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    from scmgen.loader.run import load_workspace

    settings = _bootstrap()
    try:
        result = load_workspace(settings, code, full=full, tables=tables or None, force=force, source=source)
    except ScmgenError as exc:
        _fail(exc, "load.error")
    typer.echo(f"Replica {result.duckdb_path} (load {result.load_batch_id}, high-water mark {result.high_water_mark})")
    if result.full_reason:
        typer.echo(f"Full load: {result.full_reason}")
    for kind in ("FULL", "TABLE_RELOAD", "INCREMENTAL_APPEND", "INCREMENTAL_MERGE"):
        group = [t for t in result.tables if t.load_type == kind]
        if group:
            typer.echo(f"  {kind:<20}{len(group):>3} tables  {sum(t.rows_extracted for t in group):>10,} rows read  "
                       f"{sum(t.rows_inserted for t in group):>10,} inserted  {sum(t.rows_updated for t in group):>9,} updated")
    for table in result.tables:
        if table.status != "SUCCESS" or table.reconciliation == "FAILED":
            typer.secho(f"  {table.table}: {table.status}, reconciliation {table.reconciliation} {table.message or ''}",
                        fg=typer.colors.RED)
    colour = typer.colors.GREEN if result.status in ("SUCCESS", "NOTHING_TO_DO") and result.reconciled else typer.colors.RED
    typer.secho(f"Load {result.status}; reconciliation {'PASSED' if result.reconciled else 'FAILED'}", fg=colour)
    if result.status not in ("SUCCESS", "NOTHING_TO_DO") or not result.reconciled:
        raise typer.Exit(1)


@load_app.command("status")
def load_status_command(
    code: Annotated[str, typer.Option("--workspace", "-w", help="Workspace code.")],
    source: Annotated[str, typer.Option("--source", help="oltp or sim.")] = "oltp",
) -> None:
    """Watermarks, the last load of each table and recent loader errors."""
    from scmgen.loader.run import load_status

    settings = _bootstrap()
    try:
        status = load_status(settings, code, source=source)
    except ScmgenError as exc:
        _fail(exc, "load.status.error")
    typer.echo(f"Replica {status['path']}: source build {'matches' if status['build_matches'] else 'CHANGED (next load is full)'}, "
               f"high-water mark {status['info'].get('high_water_mark')}")
    typer.echo(f"{'Table':<32}{'Strategy':<9}{'Rows':>10}  {'Watermark':<28}{'Last load':<20}{'Status':<9}Recon")
    for table, strategy, ts, pk, rows, kind, state, recon, finished in status["tables"]:
        mark = f"{ts:%Y-%m-%d %H:%M:%S} #{pk}" if ts else "-"
        typer.echo(f"{table:<32}{strategy:<9}{rows or 0:>10,}  {mark:<28}{(kind or '-'):<20}{(state or '-'):<9}{recon or '-'}")
    if status["errors"]:
        typer.echo("Recent errors:")
        for occurred, table, stage, kind, message in status["errors"][:10]:
            typer.echo(f"  {occurred:%Y-%m-%d %H:%M:%S} {table} {stage}/{kind}: {message}")


# --- ui ---------------------------------------------------------------------------------------

@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option("--port", help="Port of the control panel.")] = 8501,
) -> None:
    """Start the Streamlit control panel (D12) on http://localhost:<port>. Stop it with Ctrl+C."""
    import subprocess
    import sys
    from pathlib import Path

    import scmgen.ui

    app_path = Path(scmgen.ui.__file__).with_name("app.py")
    typer.echo(f"Control panel: http://localhost:{port}  (Ctrl+C to stop)")
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path),
                                      "--server.port", str(port), "--server.address", "localhost",
                                      "--server.headless", "true", "--browser.gatherUsageStats", "false"]))

