"""Scenario configuration (config/scenarios.yaml; master prompt §6; step 3.15)."""

from datetime import date
from pathlib import Path
import shutil

import pytest

from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.generation.config import load_generation_config
from scmgen.generation.scenarios import load_scenarios, profile_overrides
from scmgen.reports.scenarios import MEASURES

TODAY = date(2026, 11, 10)


def test_every_master_prompt_scenario_has_a_band_and_a_measure():
    scenarios = load_scenarios()["scenarios"]
    assert set(scenarios) == set(MEASURES) and len(scenarios) == 20
    assert all(spec["band"][0] <= spec["band"][1] for spec in scenarios.values())


@pytest.mark.parametrize("profile", sorted(load_scenarios()["profiles"]))
def test_every_profile_is_a_valid_set_of_overrides(profile):
    config = load_generation_config(today=TODAY, overrides=profile_overrides(profile))
    if profile == "stress":
        assert config.sales.customer_cancel_share == 0.06 and config.procurement.late_share == 0.25


def test_unknown_profile_and_missing_scenario_are_refused(tmp_path: Path):
    with pytest.raises(ConfigError, match="Unknown scenario profile"):
        profile_overrides("chaos")
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    path = tmp_path / "config" / "scenarios.yaml"
    path.write_text(path.read_text().replace("  stock_shortages:", "  stockouts:"))
    with pytest.raises(ConfigError, match="stock_shortages"):
        load_scenarios(tmp_path / "config")
