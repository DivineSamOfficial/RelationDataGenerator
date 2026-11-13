"""The legal GST set-off order (D58, Rule 88A) used by GSTR-3B (step 3.14b)."""

from decimal import Decimal as D

from scmgen.generation.engine.gst_returns import set_off


def test_igst_credit_first_then_own_heads():
    used, cash = set_off({"cgst": D(100), "sgst": D(100), "igst": D(50)},
                         {"cgst": D(30), "sgst": D(200), "igst": D(120)})
    # IGST credit: IGST 50, then CGST 70; CGST credit: CGST 30; SGST credit: SGST 100. Nothing left in cash.
    assert used == [("igst", "igst", D(50)), ("igst", "cgst", D(70)), ("cgst", "cgst", D(30)), ("sgst", "sgst", D(100))]
    assert cash == {"cgst": D(0), "sgst": D(0), "igst": D(0)}


def test_cgst_credit_never_pays_sgst():
    used, cash = set_off({"cgst": D(0), "sgst": D(80), "igst": D(0)}, {"cgst": D(500), "sgst": D(0), "igst": D(0)})
    assert used == [] and cash["sgst"] == D(80)


def test_cgst_and_sgst_credit_pay_igst_after_their_own_head():
    used, cash = set_off({"cgst": D(10), "sgst": D(10), "igst": D(100)}, {"cgst": D(50), "sgst": D(50), "igst": D(0)})
    assert used == [("cgst", "cgst", D(10)), ("cgst", "igst", D(40)), ("sgst", "sgst", D(10)), ("sgst", "igst", D(40))]
    assert cash["igst"] == D(20)
