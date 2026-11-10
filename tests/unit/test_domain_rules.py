"""Phase 3 step 3.2: pure business rules (GST, costing, identifiers, journals) without a database."""

from __future__ import annotations

import random
import re
from decimal import Decimal as D

import pytest

from scmgen.core.money import sql_divide
from scmgen.core.settings import PROJECT_ROOT
from scmgen.domain.costing import StockError, StockPosition
from scmgen.domain.gst import (
    GstError,
    document_totals,
    is_inter_state,
    net_unit_cost,
    price_exclusive,
    price_inclusive,
    shipping_charge_rate,
    split_gst,
    tax_line,
    total_gst,
    transfer_tax,
)
from scmgen.domain.identifiers import (
    CIN_PATTERN,
    GSTIN_PATTERN,
    IFSC_PATTERN,
    MOBILE_PATTERN,
    PAN_PATTERN,
    PIN_PATTERN,
    VEHICLE_PATTERN,
    IdentifierError,
    cin,
    email,
    eway_bill_number,
    gstin,
    gstin_check_character,
    ifsc,
    irn,
    is_valid_gstin,
    mobile,
    pan,
    pincode,
    tracking_number,
    vehicle_number,
)
from scmgen.domain.journal import (
    SYSTEM_ROLES,
    AccountBook,
    BusinessUnitAccounts,
    JournalBuilder,
    JournalError,
    UnbalancedJournalError,
)

# --------------------------------------------------------------------------- MySQL division (D87)


def test_sql_divide_rounds_to_dividend_scale_plus_four():
    # The case found against MySQL 26.10: 2859702.57 / 3837.994 = 745.10344986…
    assert sql_divide(D("2859702.57"), D("3837.994"), 2) == D("745.103450")      # 6 digits, as MySQL shows
    assert round(sql_divide(D("2"), D("3"), 0), 4) == D("0.6667")


# --------------------------------------------------------------------------- GST lines


def test_b2b_line_excluding_gst():
    line = price_exclusive(D("10"), D("499.5000"), D("5"), D("18"))
    assert line.taxable_amount == D("4745.25")                  # 10 × 499.50 × 0.95
    assert total_gst(line.taxable_amount, line.gst_rate) == D("854.15")
    intra, inter = split_gst(line.taxable_amount, 18, False), split_gst(line.taxable_amount, 18, True)
    assert (intra.cgst, intra.sgst, intra.igst) == (D("427.07"), D("427.07"), 0)   # 427.0725 each
    assert (inter.cgst, inter.sgst, inter.igst) == (0, 0, D("854.15"))


def test_b2c_line_including_gst_comes_back_to_the_shelf_price():
    line = price_inclusive(1, D("499"), 0, 18)                  # ₹499 MRP incl. 18% GST
    assert line.unit_price == D("422.8814") and line.taxable_amount == D("422.88")
    assert line.taxable_amount + total_gst(line.taxable_amount, 18) == D("499.00")


def test_intra_state_split_can_differ_from_order_total_by_a_paisa():
    # D39: the order carries total GST; the invoice splits it, each half rounded separately.
    assert total_gst(D("0.25"), 18) == D("0.05")                 # 0.045 → 0.05
    split = split_gst(D("0.25"), 18, inter_state=False)
    assert split.total == D("0.04")                              # 0.0225 → 0.02 twice


@pytest.mark.parametrize("kwargs, message", [
    ({"qty": 0}, "Quantity"), ({"price": 0}, "Unit price"), ({"discount": 101}, "Discount"),
    ({"gst": 51}, "GST rate"),
])
def test_impossible_lines_are_refused(kwargs, message):
    args = {"qty": 1, "price": 10, "discount": 0, "gst": 18} | kwargs
    with pytest.raises(GstError, match=message):
        price_exclusive(args["qty"], args["price"], args["discount"], args["gst"])


def test_inclusive_discount_is_capped_at_90_like_the_ddl():
    price_inclusive(1, 100, 90, 18)
    with pytest.raises(GstError, match="Discount"):
        price_inclusive(1, 100, D("90.0001"), 18)


def test_document_totals_round_to_the_rupee():
    lines = [tax_line(D("4745.25"), 18, False), tax_line(D("100.10"), 5, False)]
    totals = document_totals(lines)
    exact = D("4745.25") + D("854.14") + D("100.10") + D("5.00")   # cgst+sgst = 427.07×2 and 2.50×2
    assert totals.taxable_amount == D("4845.35") and totals.cgst_amount == totals.sgst_amount == D("429.57")
    assert exact == D("5704.49")
    assert totals.total_amount == D("5704.00") and totals.round_off_amount == D("-0.49")
    assert abs(totals.round_off_amount) <= D("0.50")


def test_document_cannot_mix_intra_and_inter_state():
    with pytest.raises(GstError, match="either"):
        document_totals([tax_line(D(100), 18, False), tax_line(D(100), 18, True)])


def test_round_off_never_exceeds_fifty_paise():
    rng = random.Random(3)
    for _ in range(2000):
        lines = [tax_line(D(rng.randint(1, 10**7)) / 100, rng.choice([0, 5, 12, 18, 28]), False)
                 for _ in range(rng.randint(1, 5))]
        totals = document_totals(lines)
        assert -D("0.50") <= totals.round_off_amount <= D("0.50") and totals.total_amount % 1 == 0


def test_place_of_supply_shipping_and_transfers():
    assert is_inter_state("KA", "MH") and not is_inter_state("KA", "KA")
    assert shipping_charge_rate([5, 18, 12]) == D("18.0000")
    tax = transfer_tax(D("12"), D("350.1234"), 18)
    assert tax.transfer_value == D("4201.48") and tax.igst_amount == D("756.27")
    assert net_unit_cost(D("4745.25"), 10) == D("474.5250")


# --------------------------------------------------------------------------- moving-average cost


def test_receipts_and_issues_follow_moving_average():
    position = StockPosition()
    position, receipt = position.receive(100, D("500"))             # 100 @ 500
    position, _ = position.receive(50, D("530"))                     # 50 @ 530 → V 76,500, Q 150
    assert position.moving_average_cost == D("510.0000")
    position, issue = position.issue(30)                             # COGS 30 × 510
    assert issue.value_change == D("-15300.00") and issue.unit_cost == D("510.0000")
    assert (position.quantity, position.value) == (D("120.000"), D("61200.00"))
    assert receipt.value_change == D("50000.00")


def test_issuing_everything_leaves_exactly_zero():
    position, _ = StockPosition().receive(3, D("10.00"))             # V = 30.00, MAC 10
    position, _ = position.receive(1, D("10.01"))                    # V = 40.01, Q = 4, MAC 10.0025
    issued = []
    for _ in range(4):
        position, movement = position.issue(1)
        issued.append(-movement.value_change)
    assert position.quantity == 0 and position.value == 0
    assert sum(issued) == D("40.01")                                 # the last issue takes what was left


def test_inventory_never_goes_negative():
    position, _ = StockPosition().receive(5, 1)
    with pytest.raises(StockError, match="never goes negative"):
        position.issue(6)
    with pytest.raises(StockError):
        StockPosition(D(1), D(0))                                    # V = 0 with Q > 0 is forbidden


def test_mac_matches_mysql_double_rounding():
    position = StockPosition(D("3837.994"), D("2859702.57"))
    assert position.moving_average_cost == D("745.1035")             # single rounding would say 745.1034


def test_found_stock_uses_mac_or_last_cost():
    empty_after_sale = StockPosition(D(0), D(0), last_unit_cost=D("42.5000"))
    position, movement = empty_after_sale.receive_found(2, reference_cost=D("40"))
    assert movement.unit_cost == D("42.5000") and position.value == D("85.00")
    _, fresh = StockPosition().receive_found(2, reference_cost=D("40"))
    assert fresh.unit_cost == D("40.0000")


def test_random_movements_keep_value_and_quantity_consistent():
    rng = random.Random(11)
    position = StockPosition()
    for _ in range(5000):
        if position.quantity == 0 or rng.random() < 0.45:
            position, _ = position.receive(rng.randint(1, 200), D(rng.randint(100, 900_000)) / 100)
        else:
            position, _ = position.issue(rng.randint(1, int(position.quantity)))
        assert (position.quantity == 0) == (position.value == 0) and position.value >= 0


# --------------------------------------------------------------------------- identifiers (D36)


def test_pan_and_gstin_are_valid_and_synthetic():
    rng = random.Random(5)
    for holder, name in [("C", "Kaveri Nexus"), ("P", "Iyer"), ("F", "Shree Traders")]:
        value = pan(rng, holder, name)
        assert PAN_PATTERN.match(value) and value.startswith("ZZX") and value[3] == holder and value[4] == name[0]
        number = gstin("29", value)
        assert GSTIN_PATTERN.match(number) and is_valid_gstin(number) and number[2:12] == value
        assert number[2:5] == "ZZX" and number[12:14] == "1Z"


def test_gstin_check_character_detects_single_changes():
    number = gstin("27", "ZZXCK4821M")
    assert is_valid_gstin(number)
    for position in range(14):
        for replacement in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if replacement != number[position]:
                changed = number[:position] + replacement + number[position + 1:]
                assert not is_valid_gstin(changed), changed      # Luhn mod 36 catches every single change


def test_other_identifiers_match_their_ddl_formats():
    rng = random.Random(9)
    for _ in range(200):
        assert CIN_PATTERN.match(cin(rng, "KA", 2019))
        assert IFSC_PATTERN.match(ifsc(rng, "KVNB"))
        assert PIN_PATTERN.match(pincode(rng, "56"))
        assert MOBILE_PATTERN.match(mobile(rng))
        assert VEHICLE_PATTERN.match(vehicle_number(rng, "KA"))
        assert re.fullmatch(r"[1-9][0-9]{11}", eway_bill_number(rng))
    assert cin(rng, "MH", 2010, listed=True).startswith("L")
    assert email("Anita Rao", "Kaveri Nexus") == "anita.rao@kaveri-nexus.example"
    assert re.fullmatch(r"[0-9a-f]{64}", irn("29ZZXCK4821M1Z7", "2026-27", "INV", "KA2627-I000001"))
    assert tracking_number("SWF", 1) == "SWF0000000001"


@pytest.mark.parametrize("call", [
    lambda: pan(random.Random(1), "X", "Name"), lambda: pan(random.Random(1), "C", "123"),
    lambda: gstin("2", "ZZXCK4821M"), lambda: gstin("29", "BAD"), lambda: ifsc(random.Random(1), "KV1B"),
    lambda: pincode(random.Random(1), "05"), lambda: email("!!!", "x"),
])
def test_bad_identifier_inputs_are_refused(call):
    with pytest.raises(IdentifierError):
        call()


# --------------------------------------------------------------------------- journals (D62, D16)


def test_balanced_journal_and_its_reversal():
    journal = (JournalBuilder("Sales invoice KA2627-I000001")
               .debit(11000, D("5600.00"), customer_id=7)
               .credit(41010, D("4745.25"), cost_center_id=3)
               .credit(22011, D("427.07")).credit(22012, D("427.07"))
               .credit(22013, D("0.00"))                            # zero IGST: no line
               .credit(49200, D("0.61"))
               .build())
    assert len(journal.lines) == 5 and journal.total_debit == journal.total_credit == D("5600.00")
    assert [line.line_number for line in journal.lines] == [1, 2, 3, 4, 5]
    reversal = journal.reversal("Cancel KA2627-I000001")
    assert reversal.lines[0].credit_amount == D("5600.00") and reversal.lines[0].customer_id == 7
    assert reversal.total_debit == reversal.total_credit


def test_unbalanced_journal_is_refused():
    builder = JournalBuilder("broken").debit(1, D("100.00")).credit(2, D("99.99"))
    with pytest.raises(UnbalancedJournalError, match="100.00"):
        builder.build()


def test_negative_amount_switches_side():
    journal = JournalBuilder("PPV favourable").debit(1, D("10.00")).debit(2, D("-2.00")).credit(3, D("8.00")).build()
    assert journal.lines[1].credit_amount == D("2.00") and journal.lines[1].debit_amount == 0


@pytest.mark.parametrize("call, message", [
    (lambda b: b.debit(1, D("1.005")), "paisa"),
    (lambda b: b.debit(1, D("1.00"), customer_id=1, supplier_id=2), "at most one party"),
    (lambda b: b.debit(1, D("1.00"), project_id=4), "Unknown journal line field"),
    (lambda b: b.debit(1, D("1.00")).build(), "two lines"),
])
def test_invalid_journal_lines_are_refused(call, message):
    with pytest.raises(JournalError, match=message):
        call(JournalBuilder("x"))


def test_system_roles_match_design_part_1b():
    text = (PROJECT_ROOT / "docs/design/phase1_part1b_master_data_and_accounting_master.md").read_text()
    section = text[text.index("## 5. Default Chart of Accounts Skeleton"):text.index("## 6. Relationship Summary")]
    assert set(re.findall(r"\[([A-Z_]{4,})\]", section)) == SYSTEM_ROLES


def test_account_book_lookups():
    book = AccountBook(
        roles={role: i for i, role in enumerate(sorted(SYSTEM_ROLES), 1)},
        business_units={1: BusinessUnitAccounts(41010, 42010, 51010)},
        tax_accounts={(1, "CGST", "OUTPUT"): 22011},
        expense_categories={4: 60300}, bank_accounts={2: 10101})
    assert book.missing_roles() == set()
    assert book.revenue(1) == 41010 and book.cogs(1) == 51010 and book.sales_returns(1) == 42010
    assert book.tax(1, "CGST", "OUTPUT") == 22011 and book.expense(4) == 60300 and book.bank(2) == 10101
    for call in (lambda: book.role("NOPE"), lambda: book.revenue(9), lambda: book.tax(1, "IGST", "OUTPUT"),
                 lambda: book.tax(1, "VAT", "OUTPUT"), lambda: AccountBook(roles={}).role("GRNI")):
        with pytest.raises(JournalError):
            call()
