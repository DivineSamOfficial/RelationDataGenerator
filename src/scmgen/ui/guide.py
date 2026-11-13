"""The Table guide (owner request O1): what each table holds, its columns, keys and relations, how it is
populated and how to query it. Descriptions and column notes come from the design docs (``docs/design``: every
table has a ``#### `name``` section with its column table, rules and population notes); keys and relations come from
the live schema; example queries from ``config/ui/table_guide.yaml`` plus generated join examples."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from scmgen.core.settings import DEFAULT_CONFIG_DIR, PROJECT_ROOT

DESIGN_DIR = PROJECT_ROOT / "docs" / "design"
_HEADING = re.compile(r"^#### (?:[0-9.]+ )?`([a-z_]+)`\s*(.*)$")
_PART = re.compile(r"phase1_part(1[a-h])_")


@dataclass
class TableDoc:
    name: str
    part: str
    module: str
    summary: str = ""
    columns: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _module(path: Path, heading_path: list[str]) -> str:
    for heading in reversed(heading_path):
        cleaned = re.sub(r"^#+\s*(\d+(\.\d+)*\.?)?\s*", "", heading).strip()
        if cleaned:
            return cleaned
    return path.stem


def load_table_docs(design_dir: Path = DESIGN_DIR) -> dict[str, TableDoc]:
    """Every ``#### `table``` section of the design docs, with its column table and bullet notes."""
    docs: dict[str, TableDoc] = {}
    for path in sorted(design_dir.glob("phase1_part1*.md")):
        part = (_PART.search(path.name).group(1) if _PART.search(path.name) else path.stem).upper()
        current: TableDoc | None = None
        headings: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _HEADING.match(line)
            if line.startswith("## ") or line.startswith("### "):
                headings = [h for h in headings if h.count("#") < line.split(" ")[0].count("#")] + [line]
                current = None
                continue
            if match:
                name = match.group(1)
                current = docs.setdefault(name, TableDoc(name, part, _module(path, headings)))
                continue
            if line.startswith("#### ") or line.startswith("---"):
                current = None
                continue
            if current is None:
                continue
            if line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if cells and cells[0] in ("Column", "Kind"):
                    continue
                if len(cells) >= 4 and cells[0].startswith("`"):
                    current.columns.append({"column": cells[0].strip("`"), "type": cells[1].strip("`"),
                                            "required": "yes" if cells[2] == "✓" else "", "notes": cells[3]})
            elif line.startswith("- "):
                current.notes.append(line[2:].strip())
            elif line.strip() and not current.summary and not line.startswith("|"):
                current.summary = line.strip()
    return docs


def load_examples(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, list[dict[str, str]]]:
    path = Path(config_dir) / "ui" / "table_guide.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {table: list(examples or []) for table, examples in (raw.get("examples") or {}).items()}


def join_example(table: str, pk: str, parents: list[tuple[str, str, str]]) -> str:
    """A generated example joining the table to up to three of its parents."""
    if not parents:
        return f"SELECT *\nFROM {table}\nORDER BY {pk} DESC\nLIMIT 20;"
    lines, joins = [f"SELECT t.*"], []
    for index, (column, parent, parent_column) in enumerate(parents[:3], 1):
        alias = f"p{index}"
        lines.append(f"     , {alias}.*  -- {parent}")
        joins.append(f"LEFT JOIN {parent} {alias} ON {alias}.{parent_column} = t.{column}")
    return "\n".join(lines) + f"\nFROM {table} t\n" + "\n".join(joins) + f"\nORDER BY t.{pk} DESC\nLIMIT 20;"


def relations_dot(table: str, parents: list[tuple[str, str, str]], children: list[tuple[str, str]]) -> str:
    """Graphviz DOT of a table with its direct parents (FK targets) and children (tables pointing at it)."""
    lines = ['digraph G {', 'rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef3fb", fontname="Helvetica"];',
             f'"{table}" [fillcolor="#c9daf8", penwidth=2];']
    for column, parent, _ in parents:
        lines.append(f'"{table}" -> "{parent}" [label="{column}", fontsize=9];')
    for child, column in children:
        lines.append(f'"{child}" -> "{table}" [label="{column}", fontsize=9, color="#888888"];')
    lines.append("}")
    return "\n".join(lines)
