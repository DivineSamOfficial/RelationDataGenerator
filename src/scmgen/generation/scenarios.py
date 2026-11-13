"""Scenario configuration (``config/scenarios.yaml``; master prompt §6; step 3.15).

``load_scenarios`` reads the scenario bands and the named profiles; ``profile_overrides`` gives a profile's
overrides of ``generation.yaml`` (dotted keys, applied like ``--set``, before any explicit ``--set``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scmgen.core.errors import ConfigError
from scmgen.generation.config import DEFAULT_CONFIG_DIR
from scmgen.reports.scenarios import MEASURES

SCENARIOS_FILE = "scenarios.yaml"


def load_scenarios(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, Any]:
    path = Path(config_dir) / SCENARIOS_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc
    scenarios, profiles = raw.get("scenarios"), raw.get("profiles")
    if not isinstance(scenarios, dict) or not isinstance(profiles, dict):
        raise ConfigError(f"{SCENARIOS_FILE} needs 'scenarios' and 'profiles' mappings")
    missing, unknown = sorted(set(MEASURES) - set(scenarios)), sorted(set(scenarios) - set(MEASURES))
    if missing or unknown:
        raise ConfigError(f"{SCENARIOS_FILE}: scenarios without a band {missing}, unknown scenarios {unknown}")
    for name, spec in scenarios.items():
        band = spec.get("band") if isinstance(spec, dict) else None
        if not (isinstance(band, list) and len(band) == 2 and all(isinstance(v, (int, float)) for v in band)
                and band[0] <= band[1]):
            raise ConfigError(f"{SCENARIOS_FILE}: scenarios.{name}.band must be [low, high]")
    for name, overrides in profiles.items():
        if not isinstance(overrides, dict):
            raise ConfigError(f"{SCENARIOS_FILE}: profiles.{name} must be a mapping of dotted keys")
    return {"scenarios": scenarios, "profiles": profiles}


def profile_overrides(name: str | None, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, Any]:
    if not name:
        return {}
    profiles = load_scenarios(config_dir)["profiles"]
    if name not in profiles:
        raise ConfigError(f"Unknown scenario profile {name!r}; known: {', '.join(sorted(profiles))}")
    return dict(profiles[name] or {})
