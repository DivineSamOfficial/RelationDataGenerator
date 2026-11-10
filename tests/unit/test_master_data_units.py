"""Phase 3 step 3.5: master-data helpers and the setup / organisation settings."""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

import pytest

from scmgen.core.errors import ConfigError
from scmgen.domain.identifiers import pincode
from scmgen.generation.config import load_generation_config
from scmgen.generation.master.catalogue import _quotas, charm_price
from scmgen.generation.master.context import city_code, domain_slug, trade_name_of

TODAY = date(2026, 11, 10)


def test_city_codes_are_short_unique_and_stable():
    taken: set[str] = set()
    codes = [city_code(name, taken) for name in ("Bengaluru", "Mumbai", "New Delhi", "Chennai", "Kolkata")]
    assert codes == ["BNG", "MMB", "NWD", "CHN", "KLK"]
    assert city_code("Bengaluru", taken) == "BN2"                    # collision gets a number


@pytest.mark.parametrize("legal, trade", [
    ("Kaveri Nexus Distribution Pvt. Ltd.", "Kaveri Nexus Distribution"),
    ("Shree Traders Private Limited", "Shree Traders"),
    ("Orion Retail Ltd", "Orion Retail"), ("Arvind & Sons LLP", "Arvind & Sons"), ("Plain Name", "Plain Name"),
])
def test_trade_name_drops_the_company_form(legal, trade):
    assert trade_name_of(legal) == trade


def test_domain_slug():
    assert domain_slug("Arvind & Sons") == "arvind-sons"


def test_city_pins_stay_low_in_their_range():
    rng = random.Random(1)
    pins = [pincode(rng, "560", 120) for _ in range(500)]
    assert all(p.startswith("560") and 1 <= int(p[3:]) <= 120 for p in pins)


def test_setup_and_organisation_load():
    config = load_generation_config(today=TODAY)
    assert config.setup.hq_city == "Bengaluru" and len(config.setup.distribution_centres) == 5
    assert config.organisation.job_levels["MANAGER"].approval_limit == 1_000_000
    assert {d.code for d in config.organisation.departments} == {"PUR", "LOG", "SAL", "MKT", "CS", "FIN", "HR", "IT"}
    assert "setup" in config.snapshot() and "organisation" in config.snapshot()


@pytest.mark.parametrize("overrides, message", [
    ({"setup.stores": "[Pune]"}, "no distribution centre"),
    ({"setup.distribution_centres": "[]"}, "at least one city"),
    ({"setup.distribution_centres": "[Mumbai, Mumbai]"}, "twice"),
    ({"setup.company_age_years": "[9, 3]"}, "min 9 is above max 3"),
    ({"setup.headquarters": "Pune"}, "unknown setting"),
    ({"organisation.warehouse_staff_share": "1.5"}, "between 0 and 1"),
    ({"master_data.customers.b2b_segments.RETAILER": "0.9"}, "add up to"),
    ({"master_data.suppliers.per_product": "[1, 3]"}, "per_product min"),
    ({"master_data.campaigns.colour": "red"}, "unknown setting"),
])
def test_invalid_setup_is_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


@pytest.mark.parametrize("raw, charm", [(37, 39), (487, 489), (1234, 1299), (24310, 24999), (5, 9)])
def test_charm_prices_end_like_indian_retail_prices(raw, charm):
    assert charm_price(Decimal(raw)) == Decimal(charm)


def test_quotas_split_exactly():
    assert _quotas(10, [Decimal(1)] * 3) == [4, 3, 3]
    assert sum(_quotas(2000, [Decimal(w) for w in (10, 4, 5, 8, 5, 6)])) == 2000
