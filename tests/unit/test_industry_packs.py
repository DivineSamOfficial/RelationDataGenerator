"""Phase 3 step 3.3: industry packs load, describe a usable catalogue, and every inconsistency is caught."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from scmgen.core.errors import WorkspaceError
from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.db.workspaces import validate_industries
from scmgen.generation.packs import GST_SLABS, PackError, available_packs, load_pack, load_packs
from scmgen.generation.seed import load_seed

SEED = load_seed()


def test_shipped_packs_load_and_coexist():
    assert available_packs() == ["beauty", "electronics", "fashion", "fmcg", "furniture", "home_kitchen",
                                 "industrial_mro", "office_stationery"]
    packs = load_packs(available_packs(), SEED)
    assert {p.business_unit_code for p in packs} == {"BEAU", "ELEC", "FASH", "FMCG", "FURN", "HOME", "INDL", "OFFC"}
    for pack in packs:
        assert len(pack.leaves) >= 10 and len(pack.brands) >= 10 and len(pack.events) >= 6
        assert sum(pack.seasonality) == Decimal(12)
        for leaf in pack.leaves:
            assert leaf.gst_before in GST_SLABS and leaf.gst_after <= leaf.gst_before
            assert all(brand.tier in leaf.mrp for brand in pack.brands_for(leaf))


def test_pack_character_matches_the_design():
    electronics, beauty, home = (load_pack(code, SEED) for code in ("electronics", "beauty", "home_kitchen"))
    assert electronics.b2b_sales_share == Decimal("0.80")                 # D2 default
    assert beauty.b2b_sales_share < Decimal("0.5")                        # B2C-heavy override
    assert all(leaf.has_expiry for leaf in beauty.leaves if leaf.code != "BEAU-ORAL-TBR")
    assert not any(leaf.has_expiry for leaf in electronics.leaves)
    assert {leaf.code for leaf in beauty.leaves if not leaf.returnable} >= {"BEAU-MKP-LIP", "BEAU-ORAL-TPS"}
    assert home.returns["transit_damage_rate"] > electronics.returns["transit_damage_rate"]   # bulky / fragile
    monitors = next(c for c in electronics.leaves if c.code == "ELEC-CMP-MON")
    assert (monitors.gst_before, monitors.gst_after) == (Decimal(28), Decimal(18))           # D17 revision
    assert electronics.top_category(monitors).code == "ELEC-CMP"
    ac = next(c for c in electronics.leaves if c.code == "ELEC-APL-ACN")
    assert ac.seasonality is not None and ac.seasonality[4] > ac.seasonality[11]           # summer peak


def test_workspace_industries_must_have_a_pack():
    assert validate_industries(["beauty", "electronics"], available_packs()) == ["beauty", "electronics"]
    with pytest.raises(WorkspaceError, match="No industry pack for: pharma"):
        validate_industries(["beauty", "pharma"], available_packs())


@pytest.fixture
def config(tmp_path: Path) -> Path:
    shutil.copytree(DEFAULT_CONFIG_DIR / "industries", tmp_path / "industries")
    return tmp_path


def _edit(config: Path, code: str, old: str, new: str) -> None:
    path = config / "industries" / f"{code}.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("old, new, message", [
    ("B2B: {NET30: 0.45, NET45: 0.20", "B2B: {NET30: 0.55, NET45: 0.20", "add up to 1.10"),
    ("B2B: {FIELD_SALES: 0.45,", "B2B: {RETAIL_STORE: 0.45,", "'RETAIL_STORE' is not allowed here"),
    ("B2C: {ADVANCE: 0.72, COD: 0.28}", "B2C: {ADVANCE: 0.72, NET30: 0.28}", "consumers get no credit"),
    ("seasonality: [1.05, 0.85", "seasonality: [2.05, 0.85", "average 1.0"),
    ("{name: Republic Day Sale, month: 1, day: 20", "{name: Republic Day Sale, month: 2, day: 29", "not a date in every year"),
    ("channels: [GOOGLE_ADS, MARKETPLACE_ADS]}", "channels: [ORGANIC_SEARCH]}", "can't carry campaigns"),
    ('hsn: "85182200", gst: {before: 18, after: 18}', 'hsn: "85183000", gst: {before: 28, after: 18}', "V-MD-19"),
    ("gst: {before: 28, after: 18}, uom: PCS,\n         mrp: {ECONOMY: [6999, 10999]",
     "gst: {before: 18, after: 28}, uom: PCS,\n         mrp: {ECONOMY: [6999, 10999]", "could then exceed MRP"),
    ("gst: {before: 18, after: 18}, uom: PCS", "gst: {before: 15, after: 15}, uom: PCS", "not a GST slab"),
    ("uom: PCS", "uom: PIECE", "unknown unit of measure"),
    ("mrp: {ECONOMY: [499, 1499], MID: [1500, 3999], PREMIUM: [4000, 19999]}", "mrp: {ECONOMY: [499, 1499]}",
     "no PREMIUM MRP band for brand AURALYN"),
    ("{code: AURALYN, name: Auralyn, tier: PREMIUM, categories: [ELEC-AUD]}",
     "{code: AURALYN, name: Auralyn, tier: PREMIUM, categories: [ELEC-XYZ]}", "not a level-1 category"),
    ("hubs: [Noida,", "hubs: [Atlantis,", "not in config/seed/cities.yaml"),
    ("warranty_months: 12, nouns: [Wireless Earbuds", "warranty_month: 12, nouns: [Wireless Earbuds", "unknown field(s) warranty_month"),
    ("marketplace_commission_percent: 12\n", "", "missing section 'marketplace_commission_percent'"),
    ("{code: NIMBUZ, name: Nimbuz", "{code: AURALYN, name: Nimbuz", "brand code 'AURALYN' is used twice"),
    ("code: electronics ", "code: gadgets ", "must equal the file name"),
    ("dispositions: {SCRAP: 0.20,", "dispositions: {SCRAP: 0.30,", "dispositions must add up to 1"),
    ("RETURN_TO_SUPPLIER: 0.45, REPAIR", "RECYCLE: 0.45, REPAIR", "RECYCLE"),
])
def test_pack_problems_are_reported(config, old, new, message):
    _edit(config, "electronics", old, new)
    with pytest.raises(PackError) as info:
        load_pack("electronics", SEED, config)
    assert message in str(info.value)


def test_packs_that_clash_are_refused_together(config):
    clone = (config / "industries" / "electronics.yaml").read_text(encoding="utf-8").replace(
        "code: electronics ", "code: gadgets ")
    (config / "industries" / "gadgets.yaml").write_text(clone, encoding="utf-8")
    load_pack("gadgets", SEED, config)                                  # fine on its own
    with pytest.raises(PackError) as info:
        load_packs(["electronics", "gadgets"], SEED, config)
    text = str(info.value)
    assert "business unit code ELEC" in text and "category code 'ELEC-AUD'" in text and "brand name 'Voltrix'" in text


def test_unknown_pack_lists_the_available_ones():
    with pytest.raises(PackError, match="Available: beauty, electronics, fashion, fmcg, furniture"):
        load_pack("pharma", SEED)


def test_b2b_only_pack_has_no_consumer_mixes():
    industrial = load_pack("industrial_mro", SEED)
    assert industrial.b2b_sales_share == 1 and set(industrial.credit_terms) == {"B2B"}
    assert set(industrial.sales_channels) == {"B2B"} and set(industrial.marketing_channels) == {"B2B"}
    fmcg = load_pack("fmcg", SEED)
    uht = next(c for c in fmcg.leaves if c.code == "FMCG-DAIRY-UHT")
    assert (uht.gst_before, uht.gst_after) == (Decimal(5), Decimal(0))          # 0% slab after the revision
    assert fmcg.returns["defective_share"] > Decimal("0.5")                       # most food returns are spoilt
    fashion = load_pack("fashion", SEED)
    assert all(band.high < 2500 for leaf in fashion.leaves if leaf.gst_after == 5 for band in leaf.mrp.values())


def test_consumer_mix_in_a_b2b_only_pack_is_refused(config):
    _edit(config, "industrial_mro", "  sales_channels:\n    B2B:",
          "  sales_channels:\n    B2C: {WEBSITE: 1.0}\n    B2B:")
    with pytest.raises(PackError, match="sales_channels.B2C is given but b2b_sales_share = 1"):
        load_pack("industrial_mro", SEED, config)


def test_defective_share_may_exceed_half_but_not_one(config):
    _edit(config, "fmcg", "defective_share: 0.60", "defective_share: 1.10")
    with pytest.raises(PackError, match="defective_share must be <= 1"):
        load_pack("fmcg", SEED, config)
