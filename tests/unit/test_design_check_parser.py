"""Phase 2 step 2.7: the schema-vs-design checker parses every design-doc form and reports each kind
of difference. Uses small synthetic design docs, plus the real docs for a structural count."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scmgen.db.design_check import (
    DEFAULT_DOCS_DIR,
    DESIGN_FILES,
    ActualColumn,
    DesignParseError,
    compare,
    normalise_type,
    parse_design,
)

PART_1B = """# 1B
#### `regions`
| Column | Type | NN | Notes |
|---|---|---|---|
| `region_id` | `SMALLINT UNSIGNED` | ✓ | PK |
| `region_name` | `VARCHAR(60)` | ✓ | UNIQUE |

#### `shops` ✱
| Column | Type | NN | Notes |
|---|---|---|---|
| `shop_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `region_id` | `SMALLINT UNSIGNED` | ✓ | FK → `regions` |
| `opened_at` / `closed_at` | `DATETIME(6)` | | |
| `is_flagship` | `BOOLEAN` | ✓ | |
| `flagship_key` | generated | | `IF(is_flagship, region_id, NULL)` STORED |
| `price` | `DECIMAL(18, 4)` | ✓ | |
"""

PART_1C = """# 1C
### 2.2 Status-history tables (standard structure)
| Column | Type | NN | Notes |
|---|---|---|---|
| `<doc>_status_history_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `<doc>_id` | as parent PK | ✓ | FK → header |
| `from_status` | `VARCHAR(30)` | | |
| `to_status` | `VARCHAR(30)` | ✓ | |

### 2.3 Amount columns on taxable lines
| Column | Type | Meaning |
|---|---|---|
| `quantity` (role-named) | `DECIMAL(18,3)` | |
| `unit_price` | `DECIMAL(18,4)` | |
| `taxable_amount` | `DECIMAL(18,2)` | |
| `cgst_amount`, `sgst_amount` | `DECIMAL(18,2)` | |
| `line_total` | `DECIMAL(18,2)` | |

- **Headers carry** `taxable_amount`, `cgst_amount`, `round_off_amount` (`DECIMAL(18,2)`) and `total_amount`.

## 3. Tables
#### `bills`
| Column | Type | NN | Notes |
|---|---|---|---|
| `bill_id` | `INT UNSIGNED` | ✓ | PK |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | header pattern |

#### `bill_lines`
| Column | Type | NN | Notes |
|---|---|---|---|
| `bill_line_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `bill_id` | `INT UNSIGNED` | ✓ | FK → `bills` |
| `unit_price` … `line_total` | | ✓ | line pattern |

#### `bill_status_history`
Standard structure (§2.2), FK `bill_id`.
"""

PART_1D = "# 1D\nNothing defined here.\n"

PART_1E = """# 1E
#### `postings`
| Column | Type | NN | Notes |
|---|---|---|---|
| `posting_id` | `BIGINT UNSIGNED` | ✓ | PK |
| *Source FKs (all `BIGINT UNSIGNED`, nullable)* | | | `bill_id`, `shop_id` |

#### `v_report` (view)
```sql
SELECT p.posting_id, SUM(x.a - x.b) OVER (PARTITION BY p.posting_id ORDER BY p.posting_id) AS running,
       p.bill_id
FROM postings p;
```
"""

PART_1G = """# 1G
## 5. System Tables
#### 5.1 `run_log`
| Column | Type | NN | Notes |
|---|---|---|---|
| `run_log_id` | `INT UNSIGNED` | ✓ | PK |

#### 5.4 `scm_control.registry` (outside)
| Column | Type | NN | Notes |
|---|---|---|---|
| `registry_id` | `INT UNSIGNED` | ✓ | PK |
| `created_at` / `updated_at` | `DATETIME(6)` | ✓ | Wall clock |

## 6. DuckDB Loader
#### `etl_history`
| Column | Type | Notes |
|---|---|---|
| `x` | `UBIGINT` | DuckDB, not checked |
"""


def _write_docs(directory: Path, **overrides: str) -> Path:
    contents = dict(zip(DESIGN_FILES, (PART_1B, PART_1C, PART_1D, PART_1E, PART_1G)))
    for filename, text_ in contents.items():
        key = next((k for k in overrides if k in filename), None)
        (directory / filename).write_text(overrides[key] if key else text_, encoding="utf-8")
    return directory


@pytest.fixture
def design(tmp_path):
    return parse_design(_write_docs(tmp_path))


def _actual_from(design) -> dict[str, dict[str, ActualColumn]]:
    """A database that matches the design exactly (types filled in where the doc leaves them out)."""
    schema = {}
    for name, table in design.items():
        schema[name] = {
            c.name: ActualColumn(c.name, c.column_type or "bigint unsigned", c.not_null, c.generated,
                                 c.primary_key, frozenset({c.fk_target}) if c.fk_target else frozenset())
            for c in table.columns.values()
        }
    return schema


# --------------------------------------------------------------------------- parsing


def test_normalise_type_matches_information_schema_spelling():
    assert normalise_type("BOOLEAN") == "tinyint(1)"
    assert normalise_type("DECIMAL(18, 4)") == "decimal(18,4)"
    assert normalise_type(" BIGINT   UNSIGNED ") == "bigint unsigned"


def test_tables_views_and_scope(design):
    assert set(design) == {"regions", "shops", "bills", "bill_lines", "bill_status_history", "postings",
                           "v_report", "run_log", "scm_control.registry"}
    assert "etl_history" not in design          # Part 1G §6 is DuckDB, out of scope
    assert design["v_report"].is_view and not design["shops"].is_view


def test_standard_audit_columns_and_soft_delete(design):
    shops, regions = design["shops"].columns, design["regions"].columns
    assert shops["deleted_at"].not_null is False and "deleted_at" not in regions
    assert regions["created_at"].not_null and regions["updated_at"].column_type == "datetime(6)"


def test_row_forms(design):
    shops = design["shops"].columns
    assert {"opened_at", "closed_at"} <= set(shops) and not shops["closed_at"].not_null
    assert shops["is_flagship"].column_type == "tinyint(1)"
    assert shops["flagship_key"].generated and shops["flagship_key"].column_type is None
    assert shops["shop_id"].primary_key and shops["region_id"].fk_target == "regions"
    assert shops["price"].column_type == "decimal(18,4)"


def test_ranges_expand_from_the_1c_amount_patterns(design):
    bill = design["bills"].columns
    assert [c for c in bill if c.endswith("amount")] == ["taxable_amount", "cgst_amount", "round_off_amount",
                                                         "total_amount"]
    line = design["bill_lines"].columns
    assert list(line)[2:8] == ["unit_price", "taxable_amount", "cgst_amount", "sgst_amount", "line_total",
                               "created_at"]
    assert line["unit_price"].column_type == "decimal(18,4)" and line["line_total"].not_null


def test_group_row_lists_nullable_columns_from_notes(design):
    postings = design["postings"].columns
    assert postings["bill_id"].column_type == "bigint unsigned" and not postings["bill_id"].not_null
    assert "shop_id" in postings


def test_status_history_template_takes_the_parent_pk_type(design):
    history = design["bill_status_history"].columns
    assert history["bill_status_history_id"].primary_key
    assert history["bill_id"].column_type == "int unsigned" and history["bill_id"].fk_target == "bills"
    assert "created_at" in history and "deleted_at" not in history


def test_view_columns_come_from_the_select_list(design):
    assert list(design["v_report"].columns) == ["posting_id", "running", "bill_id"]


def test_control_table_keeps_its_own_audit_columns(design):
    registry = design["scm_control.registry"].columns
    assert list(registry) == ["registry_id", "created_at", "updated_at"]


@pytest.mark.parametrize("part, bad, message", [
    ("1b", PART_1B + "\n#### `regions`\n| Column | Type | NN | Notes |\n|---|---|---|---|\n| `x` | `INT` | ✓ | |\n",
     "defined twice"),
    ("1b", PART_1B + "\n#### `empty`\nNo columns table.\n", "no '| Column"),
    ("1b", PART_1B.replace("| `region_name` | `VARCHAR(60)` | ✓ |", "| `region_name` | `VARCHAR(60)` | yes |"),
     "NN cell"),
    ("1c", PART_1C.replace("`unit_price` … `line_total`", "`unit_price` … `total_amount`"), "matches no"),
    ("1c", PART_1C.replace("FK `bill_id`", "FK `nothing_id`"), "no design table has primary key"),
    ("1b", PART_1B.replace("| `price` | `DECIMAL(18, 4)` | ✓ | |", "| `price` | `DECIMAL(18,4)` | ✓ | |\n"
                                                                    "| `price` | `DECIMAL(18,4)` | ✓ | |"),
     "listed twice"),
])
def test_malformed_design_is_refused(tmp_path, part, bad, message):
    with pytest.raises(DesignParseError, match=message):
        parse_design(_write_docs(tmp_path, **{f"part{part}": bad}))


# --------------------------------------------------------------------------- comparison


def test_matching_schema_has_no_differences(design):
    assert compare(design, _actual_from(design), {"v_report"}) == []


def _mutate(design, table, column, **changes):
    actual = _actual_from(design)
    actual[table][column] = replace(actual[table][column], **changes)
    return actual


@pytest.mark.parametrize("table, column, changes, kind", [
    ("shops", "price", {"column_type": "decimal(18,2)"}, "TYPE"),
    ("shops", "closed_at", {"not_null": True}, "NULLABILITY"),
    ("shops", "flagship_key", {"generated": False}, "GENERATED"),
    ("regions", "region_name", {"primary_key": True}, "PRIMARY_KEY"),
    ("shops", "region_id", {"fk_targets": frozenset()}, "FOREIGN_KEY"),
    ("shops", "region_id", {"fk_targets": frozenset({"states"})}, "FOREIGN_KEY"),
])
def test_each_column_difference_is_reported(design, table, column, changes, kind):
    differences = compare(design, _mutate(design, table, column, **changes), {"v_report"})
    assert [(d.kind, d.table, d.column) for d in differences] == [(kind, table, column)]


def test_missing_and_extra_objects_are_reported_both_ways(design):
    actual = _actual_from(design)
    del actual["regions"]
    del actual["shops"]["price"]
    actual["shops"]["surprise"] = ActualColumn("surprise", "int", False, False, False, frozenset())
    actual["v_report"]["extra"] = ActualColumn("extra", "int", False, False, False, frozenset())
    actual["mystery"] = {}
    actual["v_hidden"] = {}
    found = {(d.kind, d.table, d.column) for d in compare(design, actual, {"v_report", "v_hidden"})}
    assert found == {
        ("TABLE_MISSING_IN_DB", "regions", None),
        ("COLUMN_MISSING_IN_DB", "shops", "price"),
        ("COLUMN_NOT_IN_DESIGN", "shops", "surprise"),
        ("COLUMN_NOT_IN_DESIGN", "v_report", "extra"),
        ("TABLE_NOT_IN_DESIGN", "mystery", None),
        ("VIEW_NOT_IN_DESIGN", "v_hidden", None),
    }


def test_a_table_that_became_a_view_is_reported(design):
    found = compare(design, _actual_from(design), {"v_report", "regions"})
    assert [(d.kind, d.table) for d in found] == [("OBJECT_KIND", "regions")]


# --------------------------------------------------------------------------- the real design docs


def test_real_design_docs_define_92_tables_one_view_and_the_registry():
    design = parse_design(DEFAULT_DOCS_DIR)
    workspace = {n: t for n, t in design.items() if not n.startswith("scm_control.")}
    assert sum(not t.is_view for t in workspace.values()) == 92
    assert [n for n, t in workspace.items() if t.is_view] == ["v_general_ledger"]
    assert set(design) - set(workspace) == {"scm_control.workspaces"}
    # Every table has exactly one PK column, and nearly every column states its type.
    for table in workspace.values():
        if not table.is_view:
            assert sum(c.primary_key for c in table.columns.values()) == 1, table.name
    columns = [c for t in workspace.values() if not t.is_view for c in t.columns.values()]
    untyped = [c.name for c in columns if c.column_type is None]
    assert all(c.generated for c in columns if c.column_type is None), untyped
