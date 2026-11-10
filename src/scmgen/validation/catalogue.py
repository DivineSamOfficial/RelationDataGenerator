"""The catalogue of validation rules, read from the design documents.

Every rule the design defines is a table row ``| V-XX-NN | rule |`` (``★`` before the ID marks a
reconciliation named in the master prompt §10 / §15E). The docs stay the single source: a check in
code can only exist for a rule written there, and the report lists every rule, implemented or not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from scmgen.core.errors import ConfigError
from scmgen.core.settings import PROJECT_ROOT

DEFAULT_DOCS_DIR = PROJECT_ROOT / "docs" / "design"
_ROW = re.compile(r"^\|\s*(★\s*)?(V-([A-Z]{2})-[0-9]{2})\s*\|\s*(.+?)\s*\|\s*$")

# Module code (the XX in V-XX-NN) → readable name.
MODULES = {
    "MD": "Master data", "SY": "System", "PR": "Procurement", "IN": "Inventory", "SO": "Sales orders",
    "SH": "Shipping", "IV": "Invoicing", "RT": "Returns", "PY": "Payments", "EX": "Expenses",
    "AC": "Accounting", "AR": "Receivables", "AP": "Payables", "BK": "Bank", "TX": "Tax", "LD": "Loader",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    module: str
    text: str
    reconciliation: bool          # ★ in the design: a reconciliation named in the master prompt
    source: str                   # "file.md:line"


def load_catalogue(docs_dir: Path = DEFAULT_DOCS_DIR) -> dict[str, Rule]:
    """All rules defined in the design docs, keyed by ID."""
    rules: dict[str, Rule] = {}
    for path in sorted(docs_dir.glob("phase1_part1*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _ROW.match(line)
            if not match:
                continue
            star, rule_id, module, text = match.groups()
            if module not in MODULES:
                raise ConfigError(f"{path.name}:{number}: unknown validation module {module} in {rule_id}")
            if rule_id in rules:
                raise ConfigError(f"{path.name}:{number}: {rule_id} is defined twice (also {rules[rule_id].source})")
            rules[rule_id] = Rule(rule_id, module, text, bool(star), f"{path.name}:{number}")
    return dict(sorted(rules.items()))
