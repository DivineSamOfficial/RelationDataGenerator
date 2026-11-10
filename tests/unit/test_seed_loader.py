"""Phase 3 step 3.3: the seed loader accepts the shipped seed and reports every kind of inconsistency."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.domain.journal import SYSTEM_ROLES
from scmgen.generation.seed import SeedError, load_seed


def test_shipped_seed_loads_with_the_designed_contents():
    seed = load_seed()
    counts = {"states": 37, "account_types": 6, "departments": 10, "units_of_measure": 12, "payment_methods": 8,
              "payment_terms": 8, "sales_channels": 7, "marketing_channels": 11, "shipping_carriers": 6, "banks": 3}
    assert {name: len(getattr(seed, name)) for name in counts} == counts
    assert {row["system_role"] for row in seed.accounts if row["system_role"]} == SYSTEM_ROLES
    assert len(seed.expense_categories) >= 25 and len(seed.cities) >= 60
    # Rows of one table share one column set (the batch writer needs that).
    for table in ("payment_terms", "payment_methods", "sales_channels", "states"):
        assert len({tuple(row) for row in getattr(seed, table)}) == 1, table
    assert seed.state("DL")["intra_state_tax_component"] == "SGST"          # UT with a legislature
    assert seed.state("CH")["intra_state_tax_component"] == "UTGST"
    assert seed.account("15900")["normal_balance"] == "CREDIT"              # contra asset
    assert seed.account("61900")["requires_cost_center"] and not seed.account("11000")["requires_cost_center"]
    assert isinstance(seed.payment_methods[0]["gateway_fee_percent"], (Decimal, type(None)))
    assert str(seed.gst_rate_revision["effective_date"]) == "2025-09-22"                 # D17


@pytest.fixture
def config(tmp_path: Path) -> Path:
    shutil.copytree(DEFAULT_CONFIG_DIR / "seed", tmp_path / "seed")
    return tmp_path


def _edit(config: Path, file: str, old: str, new: str) -> None:
    path = config / "seed" / file
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= 1, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("file, old, new, message", [
    ("chart_of_accounts.yaml", "role: GRNI, ", "", "without an account (V-AC-04): GRNI"),
    ("chart_of_accounts.yaml", "role: ACCRUED_EXPENSES", "role: GRNI", "more than once: GRNI"),
    ("chart_of_accounts.yaml", "role: ROUNDING_OFF", "role: ROUNDING", "doesn't know: ROUNDING"),
    ("chart_of_accounts.yaml", '{code: "14100", name: Prepaid Expenses, type: ASSET, parent: "10000"',
     '{code: "14100", name: Prepaid Expenses, type: ASSET, parent: "11000"', "parent 11000 is postable"),
    ("chart_of_accounts.yaml", '{code: "14100"', '{code: "24100"', "starting with 1"),
    ("chart_of_accounts.yaml", '{code: "52000", name: Inventory Shrinkage & Write-offs, type: COGS, parent: "50000"',
     '{code: "52000", name: Inventory Shrinkage & Write-offs, type: COGS, parent: "40000"', "differs from parent"),
    ("chart_of_accounts.yaml", '{code: "60300", name: Rent, type: EXPENSE, parent: "60000", postable: true, cash_flow: NOT_APPLICABLE}',
     '{code: "60300", name: Rent, type: EXPENSE, parent: "60000", postable: true, cash_flow: OPERATING}', "NOT_APPLICABLE"),
    ("chart_of_accounts.yaml", 'account: "60300"', 'account: "11000"', "postable EXPENSE or COGS"),
    ("chart_of_accounts.yaml", 'default_gst_rate: "18", is_itc_eligible: true, recurrence: MONTHLY, payee_type: SUPPLIER}',
     'default_gst_rate: null, is_itc_eligible: true, recurrence: MONTHLY, payee_type: SUPPLIER}', "GST rate > 0"),
    ("chart_of_accounts.yaml", "bu_cogs:", "bu_costs:", "derived account templates"),
    ("cities.yaml", "state_code: KA, pin_prefix: \"560\"", "state_code: XX, pin_prefix: \"560\"", "unknown or unusable state"),
    ("cities.yaml", 'pin_prefix: "560"', 'pin_prefix: "56"', "3 digits"),
    ("cities.yaml", "latitude: 12.9716", "latitude: 51.5", "outside India"),
    ("reference.yaml", "bank_code: XDCB", "bank_code: DCBL", "start with X"),
    ("reference.yaml", '"https://track.swiftline.example/{awb}"', '"https://track.swiftline.com/{awb}"', ".example"),
    ("reference.yaml", "{term_code: NET15, term_name: Net 15 days, due_days: 15}", "{term_code: NET15, due_days: 15}",
     "missing term_name"),
    ("reference.yaml", "{department_code: IT, department_name: IT}", "{department_code: HR, department_name: IT}",
     "duplicate department_code"),
    ("states.yaml", 'gst_state_code: "29"', 'gst_state_code: "27"', "duplicate gst_state_code"),
    ("reference.yaml", "{category: SAAS, count: 3,", "{category: SALARIES, count: 3,", "not an expense category paid to suppliers"),
    ("reference.yaml", "{category: CLOUD, count: 2,", "{category: CLOUD, per: warehouse, count: 2,", "either per"),
])
def test_seed_problems_are_reported(config, file, old, new, message):
    _edit(config, file, old, new)
    with pytest.raises(SeedError) as info:
        load_seed(config)
    assert message in str(info.value)


def test_all_problems_are_reported_together(config):
    _edit(config, "reference.yaml", "bank_code: XDCB", "bank_code: DCBL")
    _edit(config, "cities.yaml", 'pin_prefix: "560"', 'pin_prefix: "56"')
    with pytest.raises(SeedError) as info:
        load_seed(config)
    assert "start with X" in str(info.value) and "3 digits" in str(info.value)
