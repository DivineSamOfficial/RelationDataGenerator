"""``config/simulation.yaml``: CDC capture settings and anomaly scenarios (Phase 7)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR


@dataclass(frozen=True)
class CdcConfig:
    enabled: bool
    tables: str | list[str]          # "merge", "all" or an explicit list
    batch_size: int


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    injections: list[dict[str, Any]]
    requires_sim_load: bool = False
    requires_cdc: bool = False


@dataclass(frozen=True)
class SimulationConfig:
    cdc: CdcConfig
    scenarios: dict[str, Scenario]


def load_simulation_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> SimulationConfig:
    path = Path(config_dir) / "simulation.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc
    cdc = raw.get("cdc") or {}
    tables = cdc.get("tables", "merge")
    if not (tables in ("merge", "all") or (isinstance(tables, list) and all(isinstance(t, str) for t in tables))):
        raise ConfigError("simulation.yaml: cdc.tables must be 'merge', 'all' or a list of table names")
    batch = cdc.get("batch_size", 2000)
    if isinstance(batch, bool) or not isinstance(batch, int) or not 100 <= batch <= 100_000:
        raise ConfigError("simulation.yaml: cdc.batch_size must be a whole number in [100, 100000]")
    scenarios = {}
    for name, body in ((raw.get("anomalies") or {}).get("scenarios") or {}).items():
        injections = list((body or {}).get("injections") or [])
        for injection in injections:
            if not isinstance(injection, dict) or "type" not in injection or "table" not in injection:
                raise ConfigError(f"simulation.yaml: scenario {name}: every injection needs a type and a table")
            count = injection.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
                raise ConfigError(f"simulation.yaml: scenario {name}: count must be 1-1000")
        scenarios[name] = Scenario(name, str(body.get("description", "")), injections,
                                   bool(body.get("requires_sim_load")), bool(body.get("requires_cdc")))
    return SimulationConfig(CdcConfig(bool(cdc.get("enabled", False)), tables, batch), scenarios)
