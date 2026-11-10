"""Phase 3 step 3.6: calendar, daily settings and the daily event-count helper (no database)."""

from __future__ import annotations

import random
from datetime import date

import pytest

from scmgen.core.errors import ConfigError
from scmgen.generation.config import load_generation_config
from scmgen.generation.engine.day import poisson

TODAY = date(2026, 11, 10)


def test_working_days_skip_sundays_and_national_holidays():
    daily = load_generation_config(today=TODAY).daily
    assert daily.is_working_day(date(2025, 9, 8))                   # Monday
    assert not daily.is_working_day(date(2025, 9, 7))               # Sunday
    assert not daily.is_working_day(date(2025, 8, 15))              # Independence Day
    assert daily.period_close_day == 7 and daily.campaign_lead_days == 30


def test_poisson_counts_average_to_the_mean():
    rng = random.Random(5)
    draws = [poisson(rng, 2.5) for _ in range(20_000)]
    assert abs(sum(draws) / len(draws) - 2.5) < 0.05 and min(draws) == 0
    assert poisson(rng, 0) == 0


@pytest.mark.parametrize("overrides, message", [
    ({"daily.weekly_off": "[FUNDAY]"}, "weekly_off"),
    ({"daily.national_holidays": '["02-30"]'}, "MM-DD"),
    ({"daily.period_close_day": 31}, "between 1 and 28"),
    ({"daily.discontinued_per_year": 0.9}, "between 0 and 0.5"),
    ({"daily.holidays": "[]"}, "unknown setting"),
])
def test_invalid_daily_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)
