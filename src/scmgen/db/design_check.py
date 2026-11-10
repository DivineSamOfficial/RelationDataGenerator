"""Schema-vs-design check (Phase 2 step 2.7).

The design documents in ``docs/design/`` are the baseline (CLAUDE.md rule 5). This module reads the
table definitions written there, reads the live schema from ``information_schema`` and reports every
difference, so the docs and the DDL can never drift apart silently.

What the design docs look like (and what is parsed):

* Each table is a ``#### `table_name` `` heading (✱ after it = soft-deletable, ``(view)`` = a view),
  followed by a ``| Column | Type | NN | Notes |`` table. Parts 1B–1E and 1G §5 use this form.
* Status-history tables say "Standard structure (§2.2), FK `<doc>_id`." and take their columns from
  the template in Part 1C §2.2.
* The standard audit columns ``created_at``/``updated_at`` (and ``deleted_at`` on ✱ tables) are not
  repeated per table (1B §2.1); they are added here.
* A row may name several columns at once (`` `started_at` / `finished_at` ``).
* Generated columns are typed ``generated`` or ``generated `TYPE` ``.
* Notes starting with ``PK`` mark the primary key; ``FK → `table` `` names a foreign-key target.

What is compared per column: existence (both directions), type (when the doc states one), NULL /
NOT NULL, generated or not, primary key, and that a documented ``FK → `table` `` really exists.
Column order is not compared: it carries no meaning in the design.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.errors import ScmgenError
from scmgen.core.settings import PROJECT_ROOT, Settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.sql_files import execute_raw
from scmgen.db.workspace_ddl import apply_workspace_ddl
from scmgen.db.workspaces import get_workspace

# Design parts that define MySQL tables, in reading order, each with the "## " section that holds
# them (None = the whole file). Part 1G §6 defines DuckDB metadata tables: not MySQL, so not checked.
DESIGN_FILES: dict[str, str | None] = {
    "phase1_part1b_master_data_and_accounting_master.md": None,
    "phase1_part1c_procurement_and_inventory.md": None,
    "phase1_part1d_sales_shipping_invoicing_returns.md": None,
    "phase1_part1e_expenses_accounting_ar_ap_tax.md": None,
    "phase1_part1g_generation_loader_cdc_architecture.md": "## 5. System Tables",
}
TEMPLATE_FILE = "phase1_part1c_procurement_and_inventory.md"
TEMPLATE_HEADING = "### 2.2 Status-history tables"
AMOUNT_HEADING = "### 2.3 Amount columns on taxable lines"
CONTROL_PREFIX = "scm_control."

_HEADING = re.compile(r"^####\s+(?:[\d.]+\s+)?`([a-z_.]+)`(.*)$")
_COLUMN_HEADER = re.compile(r"^\|\s*Column\s*\|\s*Type\s*\|\s*NN\s*\|")
_STANDARD_STRUCTURE = re.compile(r"Standard structure \((?:1C )?§2\.2\), FK `([a-z_]+)`")
_FK_TARGET = re.compile(r"FK\s*→\s*`([a-z_]+)`")
_BACKTICKED = re.compile(r"`([^`]+)`")


class DesignParseError(ScmgenError):
    """The design documents don't follow the format this checker understands."""


@dataclass(frozen=True)
class DesignColumn:
    name: str
    column_type: str | None      # normalised MySQL COLUMN_TYPE, None = not stated in the doc
    not_null: bool
    generated: bool
    primary_key: bool
    fk_target: str | None        # documented FK target table, None = no stated target
    source: str                  # "file.md:line"


@dataclass
class DesignTable:
    name: str
    is_view: bool
    soft_delete: bool
    source: str
    columns: dict[str, DesignColumn] = field(default_factory=dict)


@dataclass(frozen=True)
class ActualColumn:
    name: str
    column_type: str
    not_null: bool
    generated: bool
    primary_key: bool
    fk_targets: frozenset[str]


@dataclass(frozen=True)
class Difference:
    kind: str
    table: str
    column: str | None
    design: str
    actual: str
    source: str

    def describe(self) -> str:
        where = self.table if self.column is None else f"{self.table}.{self.column}"
        return f"{self.kind:<22} {where:<55} design: {self.design:<28} database: {self.actual}  [{self.source}]"


# --------------------------------------------------------------------------- design side


def normalise_type(raw: str) -> str:
    """Turn a documented type into MySQL's information_schema COLUMN_TYPE spelling."""
    value = " ".join(raw.strip().lower().split())
    if value == "boolean":
        return "tinyint(1)"
    return value.replace(", ", ",")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _parse_column_rows(lines: list[str], start: int, filename: str) -> list[tuple[list[str], int]]:
    """Rows of the markdown table whose header is at ``start``: (cells, line number)."""
    rows = []
    index = start + 2                        # skip header and |---| separator
    while index < len(lines) and lines[index].startswith("|"):
        cells = _split_row(lines[index])
        if len(cells) < 4:
            raise DesignParseError(f"{filename}:{index + 1}: column row has {len(cells)} cells")
        rows.append((cells, index + 1))
        index += 1
    return rows


@dataclass(frozen=True)
class AmountPatterns:
    """The standard amount columns of Part 1C §2.3, which other tables cite as a range.

    ``line``: taxable-line columns in order (``quantity`` … ``line_total``) with their types.
    ``header``: header-total columns in order (``taxable_amount`` … ``total_amount``).
    """

    line: tuple[tuple[str, str], ...]
    header: tuple[tuple[str, str], ...]

    def expand(self, first: str, last: str, where: str) -> list[tuple[str, str]]:
        for sequence in (self.line, self.header):
            names = [name for name, _ in sequence]
            if first in names and last in names and names.index(first) <= names.index(last):
                return list(sequence[names.index(first):names.index(last) + 1])
        raise DesignParseError(f"{where}: range `{first}` … `{last}` matches no 1C §2.3 pattern")


def _amount_patterns(docs_dir: Path) -> AmountPatterns:
    lines = (docs_dir / TEMPLATE_FILE).read_text(encoding="utf-8").splitlines()
    start = next((i for i, s in enumerate(lines) if s.startswith(AMOUNT_HEADING)), None)
    if start is None:
        raise DesignParseError(f"{TEMPLATE_FILE}: amount pattern ({AMOUNT_HEADING}) not found")
    line: list[tuple[str, str]] = []
    header: list[tuple[str, str]] = []
    index = start + 1
    while index < len(lines) and not lines[index].startswith("#"):
        text_line = lines[index]
        if text_line.startswith("| `"):
            cells = _split_row(text_line)
            column_type = normalise_type(_BACKTICKED.findall(cells[1])[0])
            line.extend((name, column_type) for name in _BACKTICKED.findall(cells[0]))
        elif text_line.startswith("- **Headers carry**"):
            header = [(name, "decimal(18,2)") for name in _BACKTICKED.findall(text_line)
                      if name.endswith("_amount")]
        index += 1
    if not line or not header:
        raise DesignParseError(f"{TEMPLATE_FILE}: amount pattern incomplete (line {len(line)}, header {len(header)})")
    return AmountPatterns(tuple(line), tuple(header))


def _row_columns(cells: list[str], source: str, patterns: AmountPatterns | None) -> list[DesignColumn]:
    """The column(s) one design row stands for.

    Three row forms exist: a normal row (one or more backticked names, e.g. `` `a` / `b` ``); a range
    `` `unit_price` … `line_total` `` citing the 1C §2.3 pattern; and an italic group row whose
    columns are listed in Notes (``*Source FKs (all `BIGINT UNSIGNED`, nullable)*``).
    """
    first, type_cell, nn_cell, notes = cells[0], cells[1], cells[2], " | ".join(cells[3:])
    if nn_cell not in ("", "✓"):
        raise DesignParseError(f"{source}: NN cell must be ✓ or empty, found {nn_cell!r}")
    not_null = nn_cell == "✓"
    named = _BACKTICKED.findall(first)

    if "…" in first:
        if patterns is None or len(named) != 2:
            raise DesignParseError(f"{source}: cannot expand range {first!r}")
        stated = _BACKTICKED.findall(type_cell)
        return [DesignColumn(name, normalise_type(stated[0]) if stated else pattern_type, not_null,
                             False, False, None, source)
                for name, pattern_type in patterns.expand(named[0], named[1], source)]

    if first.startswith("*"):
        if not named:
            raise DesignParseError(f"{source}: group row {first!r} states no type")
        return [DesignColumn(name, normalise_type(named[0]), False, False, False, None, source)
                for name in _BACKTICKED.findall(notes)]

    if not named:
        raise DesignParseError(f"{source}: no column name in {first!r}")
    generated = type_cell.startswith("generated")
    typed = _BACKTICKED.findall(type_cell)
    column_type = normalise_type(typed[0]) if typed else None   # None: "generated" alone, "as parent PK"
    fk = _FK_TARGET.search(notes)
    return [
        DesignColumn(
            name=name,
            column_type=column_type,
            not_null=not_null,
            generated=generated,
            primary_key=bool(re.match(r"^\**PK\b", notes)),
            fk_target=fk.group(1) if fk else None,
            source=source,
        )
        for name in named
    ]


def _standard_columns(soft_delete: bool, source: str) -> list[DesignColumn]:
    names = [("created_at", True), ("updated_at", True)] + ([("deleted_at", False)] if soft_delete else [])
    return [DesignColumn(name, "datetime(6)", nn, False, False, None, f"{source} (1B §2.1)") for name, nn in names]


def _view_columns(lines: list[str], start: int) -> list[str]:
    """Output column names of the ```sql SELECT … FROM``` block following a view heading."""
    index = start
    while index < len(lines) and not lines[index].startswith("```sql"):
        index += 1
    body: list[str] = []
    index += 1
    while index < len(lines) and not lines[index].startswith("```"):
        body.append(lines[index])
        index += 1
    sql = " ".join(body)
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, flags=re.S)
    if not match:
        raise DesignParseError(f"view at line {start + 1}: no SELECT … FROM found")
    items, depth, current = [], 0, ""
    for char in match.group(1):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    names = []
    for item in items:
        alias = re.search(r"\bAS\s+([a-z_]+)\s*$", item.strip(), flags=re.I)
        names.append(alias.group(1) if alias else item.strip().split(".")[-1])
    return names


def _status_history_template(docs_dir: Path) -> list[tuple[list[str], int]]:
    lines = (docs_dir / TEMPLATE_FILE).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(TEMPLATE_HEADING):
            for inner in range(index, len(lines)):
                if _COLUMN_HEADER.match(lines[inner]):
                    return _parse_column_rows(lines, inner, TEMPLATE_FILE)
    raise DesignParseError(f"{TEMPLATE_FILE}: status-history template ({TEMPLATE_HEADING}) not found")


def parse_design(docs_dir: Path) -> dict[str, DesignTable]:
    """All tables and views defined in the design docs, keyed by name.

    Control-schema tables keep their ``scm_control.`` prefix in the key.
    """
    template = _status_history_template(docs_dir)
    patterns = _amount_patterns(docs_dir)
    tables: dict[str, DesignTable] = {}
    pending_history: list[tuple[DesignTable, str]] = []

    for filename, section_title in DESIGN_FILES.items():
        lines = (docs_dir / filename).read_text(encoding="utf-8").splitlines()
        in_scope = section_title is None
        index = 0
        while index < len(lines):
            if section_title is not None and lines[index].startswith("## "):
                in_scope = lines[index].startswith(section_title)
            heading = _HEADING.match(lines[index])
            if not heading or not in_scope:
                index += 1
                continue
            name, rest = heading.group(1), heading.group(2)
            source = f"{filename}:{index + 1}"
            if name in tables:
                raise DesignParseError(f"{source}: table {name} is defined twice (also {tables[name].source})")
            table = DesignTable(name=name, is_view="(view)" in rest, soft_delete="✱" in rest, source=source)
            tables[name] = table

            # The section runs until the next heading of level 1-4.
            end = index + 1
            while end < len(lines) and not re.match(r"^#{1,4}\s", lines[end]):
                end += 1
            section = lines[index + 1:end]

            if table.is_view:
                for column in _view_columns(lines, index + 1):
                    table.columns[column] = DesignColumn(column, None, False, False, False, None, source)
            elif (standard := next((_STANDARD_STRUCTURE.search(s) for s in section if _STANDARD_STRUCTURE.search(s)), None)):
                pending_history.append((table, standard.group(1)))
            else:
                header_at = next((index + 1 + i for i, s in enumerate(section) if _COLUMN_HEADER.match(s)), None)
                if header_at is None:
                    raise DesignParseError(f"{source}: no '| Column | Type | NN | Notes |' table under {name}")
                for cells, line_no in _parse_column_rows(lines, header_at, filename):
                    for column in _row_columns(cells, f"{filename}:{line_no}", patterns):
                        if column.name in table.columns:
                            raise DesignParseError(f"{column.source}: column {name}.{column.name} listed twice")
                        table.columns[column.name] = column
                is_control = name.startswith(CONTROL_PREFIX)
                if not is_control:   # the control registry lists its audit columns itself
                    for column in _standard_columns(table.soft_delete, source):
                        table.columns.setdefault(column.name, column)
            index = end

    # Status-history tables: instantiate the template now that every parent PK type is known.
    pk_types = {
        column.name: column.column_type
        for table in tables.values() for column in table.columns.values() if column.primary_key
    }
    for table, parent_fk in pending_history:
        doc = parent_fk.removesuffix("_id")
        parent_table = next((t.name for t in tables.values()
                             for c in t.columns.values() if c.primary_key and c.name == parent_fk), None)
        if parent_table is None or pk_types.get(parent_fk) is None:
            raise DesignParseError(f"{table.source}: no design table has primary key {parent_fk}")
        for cells, line_no in template:
            source = f"{TEMPLATE_FILE}:{line_no} via {table.source}"
            for column in _row_columns([cells[0].replace("<doc>", doc), *cells[1:]], source, None):
                if column.name == parent_fk:
                    column = DesignColumn(column.name, pk_types[parent_fk], column.not_null, False,
                                          False, parent_table, source)
                table.columns[column.name] = column
        for column in _standard_columns(False, table.source):
            table.columns.setdefault(column.name, column)
    return tables


# --------------------------------------------------------------------------- database side


def read_schema(conn: Connection, schema: str) -> tuple[dict[str, dict[str, ActualColumn]], set[str]]:
    """Columns per base table and the set of views in ``schema``."""
    kinds = dict(conn.execute(text(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = :s"),
        {"s": schema}).all())
    primary = {(t, c) for t, c in conn.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = :s AND CONSTRAINT_NAME = 'PRIMARY'"), {"s": schema})}
    foreign: dict[tuple[str, str], set[str]] = {}
    for t, c, target in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL"), {"s": schema}):
        foreign.setdefault((t, c), set()).add(target)
    columns: dict[str, dict[str, ActualColumn]] = {}
    for t, c, column_type, nullable, extra in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :s ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"s": schema}):
        columns.setdefault(t, {})[c] = ActualColumn(
            name=c,
            column_type=column_type.lower(),
            not_null=nullable == "NO",
            generated="GENERATED" in extra.upper() and "DEFAULT_GENERATED" not in extra.upper(),
            primary_key=(t, c) in primary,
            fk_targets=frozenset(foreign.get((t, c), set())),
        )
    views = {t for t, kind in kinds.items() if kind == "VIEW"}
    for t, kind in kinds.items():
        columns.setdefault(t, {})
    return columns, views


# --------------------------------------------------------------------------- comparison


def _flag(value: bool, yes: str, no: str) -> str:
    return yes if value else no


def compare(design: Mapping[str, DesignTable], actual: Mapping[str, Mapping[str, ActualColumn]],
            views: Iterable[str]) -> list[Difference]:
    """Every difference between the design tables and the schema, in a stable order."""
    views = set(views)
    found: list[Difference] = []
    for name in sorted(set(design) - set(actual)):
        found.append(Difference("TABLE_MISSING_IN_DB", name, None, "defined", "absent", design[name].source))
    for name in sorted(set(actual) - set(design)):
        kind = "VIEW_NOT_IN_DESIGN" if name in views else "TABLE_NOT_IN_DESIGN"
        found.append(Difference(kind, name, None, "absent", "exists", "-"))

    for name in sorted(set(design) & set(actual)):
        table, columns = design[name], actual[name]
        if table.is_view != (name in views):
            found.append(Difference("OBJECT_KIND", name, None, _flag(table.is_view, "view", "table"),
                                    _flag(name in views, "view", "table"), table.source))
        for column_name in table.columns:
            if column_name not in columns:
                found.append(Difference("COLUMN_MISSING_IN_DB", name, column_name, "defined", "absent",
                                        table.columns[column_name].source))
        for column_name in columns:
            if column_name not in table.columns:
                found.append(Difference("COLUMN_NOT_IN_DESIGN", name, column_name, "absent",
                                        columns[column_name].column_type, table.source))
        if table.is_view:
            continue                          # view column types and nullability are derived
        for column_name, want in table.columns.items():
            have = columns.get(column_name)
            if have is None:
                continue
            if want.column_type is not None and want.column_type != have.column_type:
                found.append(Difference("TYPE", name, column_name, want.column_type, have.column_type, want.source))
            if want.not_null != have.not_null:
                found.append(Difference("NULLABILITY", name, column_name, _flag(want.not_null, "NOT NULL", "NULL"),
                                        _flag(have.not_null, "NOT NULL", "NULL"), want.source))
            if want.generated != have.generated:
                found.append(Difference("GENERATED", name, column_name, _flag(want.generated, "generated", "stored value"),
                                        _flag(have.generated, "generated", "stored value"), want.source))
            if want.primary_key != have.primary_key:
                found.append(Difference("PRIMARY_KEY", name, column_name, _flag(want.primary_key, "PK", "not PK"),
                                        _flag(have.primary_key, "PK", "not PK"), want.source))
            if want.fk_target is not None and want.fk_target not in have.fk_targets:
                found.append(Difference("FOREIGN_KEY", name, column_name, f"FK → {want.fk_target}",
                                        ", ".join(sorted(have.fk_targets)) or "no FK", want.source))
    return found


def check_schema(conn: Connection, schema: str, docs_dir: Path, *, control: bool = False) -> list[Difference]:
    """Compare ``schema`` with the design: workspace tables, or (``control=True``) the control registry."""
    design = parse_design(docs_dir)
    if control:
        selected = {n.removeprefix(CONTROL_PREFIX): t for n, t in design.items() if n.startswith(CONTROL_PREFIX)}
    else:
        selected = {n: t for n, t in design.items() if not n.startswith(CONTROL_PREFIX)}
    columns, views = read_schema(conn, schema)
    return compare(selected, columns, views)


# --------------------------------------------------------------------------- orchestration

DEFAULT_DOCS_DIR = PROJECT_ROOT / "docs" / "design"
# Longer than any workspace code allows (max 10 characters), so it can never clash with a workspace.
SCRATCH_CODE = "designcheck"


@dataclass(frozen=True)
class CheckedScope:
    label: str                 # "workspace tables" / "control registry"
    schema: str
    tables: int                # design tables compared in this scope
    views: int                 # design views compared in this scope
    differences: list[Difference]

    def summary(self) -> str:
        views = f" + {self.views} view{'s' if self.views != 1 else ''}" if self.views else ""
        return f"{self.label}: {self.tables} design tables{views} vs {self.schema}"


def run_design_check(settings: Settings, docs_dir: Path = DEFAULT_DOCS_DIR,
                     workspace: str | None = None) -> list[CheckedScope]:
    """Check the workspace tables and the control registry against the design.

    Without ``workspace`` the packaged DDL is built into a throwaway schema (dropped afterwards), so the
    check answers "does the DDL match the design?". With ``workspace`` the live ``_oltp`` schema of that
    workspace is checked instead, which also catches drift in an existing database.
    """
    design = parse_design(docs_dir)
    in_workspace = [t for n, t in design.items() if not n.startswith(CONTROL_PREFIX)]
    workspace_tables = sum(not t.is_view for t in in_workspace)
    workspace_views = sum(t.is_view for t in in_workspace)
    control_tables = len(design) - len(in_workspace)
    engine = create_mysql_engine(settings, "app")
    scopes: list[CheckedScope] = []
    try:
        with engine.connect() as conn:
            if workspace is None:
                schema = f"{settings.schemas.workspace_prefix}{SCRATCH_CODE}_oltp"
                execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
                execute_raw(conn, f"CREATE DATABASE `{schema}`")
                try:
                    apply_workspace_ddl(conn, schema)
                    differences = check_schema(conn, schema, docs_dir)
                finally:
                    execute_raw(conn, f"DROP DATABASE IF EXISTS `{schema}`")
                label = "workspace tables (packaged DDL)"
            else:
                schema = get_workspace(settings, workspace).oltp_schema
                differences = check_schema(conn, schema, docs_dir)
                label = f"workspace tables ({workspace})"
            scopes.append(CheckedScope(label, schema, workspace_tables, workspace_views, differences))
            control = settings.schemas.control_schema
            scopes.append(CheckedScope("control registry", control, control_tables, 0,
                                       check_schema(conn, control, docs_dir, control=True)))
    finally:
        engine.dispose()
    return scopes
