"""Workspace lifecycle: create / list / show / delete (Part 1A Addendum A2, Part 1G §4).

A workspace is one company: a clean schema scm_<code>_oltp (all business tables), a simulation schema
scm_<code>_sim (filled in Phase 7), two DuckDB files, and one row in scm_control.workspaces.

Everything runs as scm_app (D8). MySQL DDL can't be rolled back, so `create_workspace` cleans up after
itself on failure: no half-built workspace is ever left behind.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, text

from scmgen import __version__
from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import PROJECT_ROOT, Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw, read_sql
from scmgen.db.workspace_ddl import apply_workspace_ddl, workspace_ddl_files
from scmgen.generation.packs import available_packs

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[a-z][a-z0-9]{1,9}$")  # same rule as the registry CHECK and companies.company_code
INDUSTRY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")  # same rule as business_units.industry_pack_code
RESERVED_CODES = {"control", "test", "sys", "mysql"}
MAX_SEED = 2**63 - 1


@dataclass(frozen=True)
class WorkspaceNames:
    code: str
    oltp_schema: str
    sim_schema: str
    duckdb_path: Path
    duckdb_sim_path: Path


@dataclass(frozen=True)
class WorkspaceInfo:
    code: str
    company_name: str
    industries: list[str]
    oltp_schema: str
    sim_schema: str
    duckdb_path: str
    status: str
    base_seed: int
    build_id: str
    created_at: datetime
    last_committed_through: datetime | None
    oltp_tables: int | None  # live count; None = schema missing
    sim_tables: int | None


# --- validation and naming (pure) ----------------------------------------------------------

def validate_code(code: str) -> str:
    code = code.strip()
    if not CODE_RE.match(code):
        raise WorkspaceError(
            f"Invalid workspace code {code!r}: 2-10 characters, lower-case letters and digits, starting with a letter"
        )
    if code in RESERVED_CODES:
        raise WorkspaceError(f"Workspace code {code!r} is reserved")
    return code


def validate_industries(industries: list[str], available: list[str] | None = None) -> list[str]:
    """Clean and check industry codes; with ``available``, each must be a shipped industry pack."""
    cleaned = [item.strip() for item in industries if item.strip()]
    if not cleaned:
        raise WorkspaceError("At least one industry is required (e.g. --industries beauty,electronics)")
    bad = [item for item in cleaned if not INDUSTRY_RE.match(item)]
    if bad:
        raise WorkspaceError(f"Invalid industry code(s): {', '.join(bad)} (lower-case letters, digits, underscores)")
    if len(set(cleaned)) != len(cleaned):
        raise WorkspaceError("Industries must not repeat")
    if available is not None:
        unknown = [item for item in cleaned if item not in available]
        if unknown:
            raise WorkspaceError(f"No industry pack for: {', '.join(unknown)} (available: {', '.join(available)})")
    return cleaned


def workspace_names(settings: Settings, code: str) -> WorkspaceNames:
    prefix = settings.schemas.workspace_prefix
    duckdb_dir = settings.data_dir / "duckdb"
    return WorkspaceNames(
        code=code,
        oltp_schema=f"{prefix}{code}_oltp",
        sim_schema=f"{prefix}{code}_sim",
        duckdb_path=duckdb_dir / f"{code}.duckdb",
        duckdb_sim_path=duckdb_dir / f"{code}_sim.duckdb",
    )


def ddl_fingerprint() -> dict[str, object]:
    """Which DDL files built the schema and a hash of their exact contents (detects schema drift later)."""
    files = workspace_ddl_files()
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.encode())
        digest.update(read_sql(path).encode())
    return {"ddl_files": [p.rsplit("/", 1)[-1] for p in files], "ddl_sha256": digest.hexdigest()}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


# --- database helpers -----------------------------------------------------------------------

@contextmanager
def workspace_lock(conn: Connection, code: str) -> Iterator[None]:
    """MySQL named lock: one create/delete per workspace at a time (1G §2.4). Fails fast if busy."""
    name = f"scmgen:workspace:{code}"
    acquired = conn.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar()
    if acquired != 1:
        raise WorkspaceError(f"Workspace '{code}' is busy: another scmgen operation holds its lock")
    try:
        yield
    finally:
        conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


def _registry(settings: Settings) -> str:
    return f"`{settings.schemas.control_schema}`.workspaces"


def _require_registry(conn: Connection, settings: Settings) -> None:
    exists = conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s AND table_name = 'workspaces'"),
        {"s": settings.schemas.control_schema},
    ).scalar_one()
    if not exists:
        raise WorkspaceError("The workspace registry doesn't exist yet: run `scmgen db init` first")


def _schema_table_count(conn: Connection, schema: str) -> int | None:
    exists = conn.execute(
        text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema}
    ).scalar_one()
    if not exists:
        return None
    return conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s AND table_type = 'BASE TABLE'"),
        {"s": schema},
    ).scalar_one()


def _fetch(conn: Connection, settings: Settings, code: str) -> dict | None:
    row = conn.execute(
        text(
            f"SELECT workspace_code, company_name, industries, oltp_schema, sim_schema, duckdb_path, "
            f"workspace_status, base_seed, build_id, created_at, last_committed_through "
            f"FROM {_registry(settings)} WHERE workspace_code = :c"
        ),
        {"c": code},
    ).mappings().first()
    return dict(row) if row else None


def _to_info(conn: Connection, row: dict) -> WorkspaceInfo:
    industries = row["industries"]
    return WorkspaceInfo(
        code=row["workspace_code"],
        company_name=row["company_name"],
        industries=json.loads(industries) if isinstance(industries, str) else list(industries),
        oltp_schema=row["oltp_schema"],
        sim_schema=row["sim_schema"],
        duckdb_path=row["duckdb_path"],
        status=row["workspace_status"],
        base_seed=int(row["base_seed"]),
        build_id=row["build_id"],
        created_at=row["created_at"],
        last_committed_through=row["last_committed_through"],
        oltp_tables=_schema_table_count(conn, row["oltp_schema"]),
        sim_tables=_schema_table_count(conn, row["sim_schema"]),
    )


def _create_schema(conn: Connection, settings: Settings, schema: str) -> None:
    execute_raw(
        conn,
        f"CREATE DATABASE `{schema}` CHARACTER SET {settings.mysql.charset} COLLATE {settings.mysql.collation}",
    )


# --- operations -----------------------------------------------------------------------------

def create_workspace(
    settings: Settings,
    code: str,
    company_name: str,
    industries: list[str],
    base_seed: int | None = None,
) -> WorkspaceInfo:
    """Register a workspace and build its schemas (empty: data generation is Phase 3)."""
    code = validate_code(code)
    industries = validate_industries(industries, available_packs())
    company_name = company_name.strip()
    if not 1 <= len(company_name) <= 200:
        raise WorkspaceError("Company name must be 1-200 characters")
    if base_seed is None:
        base_seed = secrets.randbelow(MAX_SEED)  # recorded, so a Rebuild reproduces the data (D14, D65)
    if not 0 <= base_seed <= MAX_SEED:
        raise WorkspaceError(f"Seed must be between 0 and {MAX_SEED}")
    names = workspace_names(settings, code)

    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn, workspace_lock(conn, code):
            _require_registry(conn, settings)
            existing = _fetch(conn, settings, code)
            if existing:
                raise WorkspaceError(
                    f"Workspace '{code}' already exists (status {existing['workspace_status']}). "
                    "Choose another code, or delete it first (use --purge to free an archived code)"
                )
            for schema in (names.oltp_schema, names.sim_schema):
                if _schema_table_count(conn, schema) is not None:
                    raise WorkspaceError(
                        f"Schema `{schema}` already exists but isn't registered. Drop it or choose another code"
                    )

            snapshot = {
                "company_name": company_name,
                "industries": industries,
                "app_version": __version__,
                **ddl_fingerprint(),
            }
            conn.execute(
                text(
                    f"INSERT INTO {_registry(settings)} (workspace_code, company_name, industries, oltp_schema, "
                    f"sim_schema, duckdb_path, duckdb_sim_path, base_seed, build_id, config_snapshot, workspace_status) "
                    f"VALUES (:code, :company, :industries, :oltp, :sim, :duck, :duck_sim, :seed, :build, :config, 'CREATING')"
                ),
                {
                    "code": code,
                    "company": company_name,
                    "industries": json.dumps(industries),
                    "oltp": names.oltp_schema,
                    "sim": names.sim_schema,
                    "duck": _display_path(names.duckdb_path),
                    "duck_sim": _display_path(names.duckdb_sim_path),
                    "seed": base_seed,
                    "build": str(uuid.uuid4()),
                    "config": json.dumps(snapshot),
                },
            )
            conn.commit()
            log.info("workspace registered", extra={"event": "workspace.create.start", "workspace": code})

            created: list[str] = []
            try:
                _create_schema(conn, settings, names.oltp_schema)
                created.append(names.oltp_schema)
                statements = apply_workspace_ddl(conn, names.oltp_schema)
                _create_schema(conn, settings, names.sim_schema)
                created.append(names.sim_schema)
                conn.execute(
                    text(f"UPDATE {_registry(settings)} SET workspace_status = 'READY' WHERE workspace_code = :c"),
                    {"c": code},
                )
                conn.commit()
            except Exception as exc:
                _cleanup_failed_create(conn, settings, code, created)
                raise WorkspaceError(f"Creating workspace '{code}' failed and was rolled back: {exc}") from exc

            log.info(
                "workspace ready",
                extra={"event": "workspace.create.done", "workspace": code, "ddl_statements": statements},
            )
            return _to_info(conn, _fetch(conn, settings, code))
    finally:
        engine.dispose()


def _cleanup_failed_create(conn: Connection, settings: Settings, code: str, created: list[str]) -> None:
    """Undo a failed create: drop schemas built so far and remove the registry row."""
    try:
        conn.rollback()
        for schema in reversed(created):
            execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
        conn.execute(text(f"DELETE FROM {_registry(settings)} WHERE workspace_code = :c"), {"c": code})
        conn.commit()
        log.warning("failed workspace create cleaned up", extra={"event": "workspace.create.rollback", "workspace": code})
    except Exception:  # pragma: no cover - last resort: leave a visible FAILED marker
        log.exception("cleanup after failed create also failed", extra={"event": "workspace.create.rollback_failed"})
        conn.execute(
            text(f"UPDATE {_registry(settings)} SET workspace_status = 'FAILED' WHERE workspace_code = :c"), {"c": code}
        )
        conn.commit()


def list_workspaces(settings: Settings) -> list[WorkspaceInfo]:
    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn:
            _require_registry(conn, settings)
            codes = conn.execute(text(f"SELECT workspace_code FROM {_registry(settings)} ORDER BY workspace_code")).scalars()
            return [_to_info(conn, _fetch(conn, settings, code)) for code in list(codes)]
    finally:
        engine.dispose()


def get_workspace(settings: Settings, code: str) -> WorkspaceInfo:
    code = validate_code(code)
    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn:
            _require_registry(conn, settings)
            row = _fetch(conn, settings, code)
            if row is None:
                raise WorkspaceError(f"No workspace '{code}'. See `scmgen workspace list`")
            return _to_info(conn, row)
    finally:
        engine.dispose()


def delete_workspace(settings: Settings, code: str, purge: bool = False) -> str:
    """Drop the workspace's schemas and DuckDB files. The registry row is kept as ARCHIVED (history),
    or removed entirely with purge=True so the code can be reused. Returns the final state."""
    code = validate_code(code)
    names = workspace_names(settings, code)
    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn, workspace_lock(conn, code):
            _require_registry(conn, settings)
            row = _fetch(conn, settings, code)
            if row is None:
                raise WorkspaceError(f"No workspace '{code}'. See `scmgen workspace list`")
            status = row["workspace_status"]
            if status in ("GENERATING", "REBUILDING"):
                raise WorkspaceError(f"Workspace '{code}' is {status}: wait for the run to finish or cancel it first")
            if status == "ARCHIVED" and not purge:
                raise WorkspaceError(f"Workspace '{code}' is already archived. Use --purge to remove its registry entry")

            for schema in (row["oltp_schema"], row["sim_schema"]):
                execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
            for duck in (names.duckdb_path, names.duckdb_sim_path):
                duck.unlink(missing_ok=True)
                duck.with_name(duck.name + ".wal").unlink(missing_ok=True)  # DuckDB write-ahead log

            if purge:
                conn.execute(text(f"DELETE FROM {_registry(settings)} WHERE workspace_code = :c"), {"c": code})
                outcome = "PURGED"
            else:
                conn.execute(
                    text(f"UPDATE {_registry(settings)} SET workspace_status = 'ARCHIVED' WHERE workspace_code = :c"),
                    {"c": code},
                )
                outcome = "ARCHIVED"
            conn.commit()
            log.info("workspace deleted", extra={"event": "workspace.delete", "workspace": code, "outcome": outcome})
            return outcome
    finally:
        engine.dispose()


# --- validation gate (D76) ----------------------------------------------------------------------

def record_validation_outcome(settings: Settings, code: str, passed: bool) -> str:
    """Flag a READY workspace NEEDS_ATTENTION when its clean data fails validation, and clear the flag
    when it passes again (D76). Other states (GENERATING, ARCHIVED …) are left alone. Returns the status."""
    code = validate_code(code)
    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn:
            _require_registry(conn, settings)
            row = _fetch(conn, settings, code)
            if row is None:
                raise WorkspaceError(f"No workspace '{code}'. See `scmgen workspace list`")
            status = row["workspace_status"]
            new_status = {("READY", False): "NEEDS_ATTENTION", ("NEEDS_ATTENTION", True): "READY"}.get((status, passed), status)
            if new_status != status:
                conn.execute(text(f"UPDATE {_registry(settings)} SET workspace_status = :s, updated_at = NOW(6) "
                                  "WHERE workspace_code = :c"), {"s": new_status, "c": code})
                conn.commit()
                log.info("workspace validation status changed",
                         extra={"event": "workspace.validation", "workspace": code, "status": new_status})
            return new_status
    finally:
        engine.dispose()


def ensure_validated(settings: Settings, code: str, force: bool = False) -> WorkspaceInfo:
    """Gate for Continue and the DuckDB load (D76): refuse a workspace whose clean data failed validation
    unless ``force``. Returns the workspace."""
    info = get_workspace(settings, code)
    if info.status == "NEEDS_ATTENTION" and not force:
        raise WorkspaceError(f"Workspace '{code}' failed its last validation (NEEDS_ATTENTION). Fix the data and run "
                             f"`scmgen validate -w {code}`, or pass --force to continue anyway")
    return info


def set_workspace_status(settings: Settings, code: str, status: str, last_committed_through=None) -> None:
    """Move a workspace to ``status`` (e.g. GENERATING while a run writes), optionally recording the
    latest committed simulated time for listings (1G §5.4)."""
    code = validate_code(code)
    engine = create_mysql_engine(settings, role="app")
    try:
        with engine.connect() as conn:
            assignments = "workspace_status = :s, updated_at = NOW(6)"
            if last_committed_through is not None:
                assignments += ", last_committed_through = :t"
            result = conn.execute(text(f"UPDATE {_registry(settings)} SET {assignments} WHERE workspace_code = :c"),
                                  {"s": status, "t": last_committed_through, "c": code})
            if result.rowcount != 1:
                raise WorkspaceError(f"No workspace '{code}'. See `scmgen workspace list`")
            conn.commit()
    finally:
        engine.dispose()
