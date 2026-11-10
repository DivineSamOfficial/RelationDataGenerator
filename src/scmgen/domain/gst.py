"""GST arithmetic for every taxable document line and header (1C §2.3, D39, D42, D79, D84, D87).

Each function returns exactly what the DDL's CHECK formulas compute, so a row built from it is
always accepted:

* ex-GST price (B2B, purchases):  taxable = ROUND(qty × unit_price × (1 − disc/100), 2)
* GST-inclusive price (B2C, D79): unit_price = ROUND(incl / (1 + rate/100), 4)
                                  taxable    = ROUND(qty × incl × (1 − disc/100) / (1 + rate/100), 2)
* order lines carry total GST (D39): tax = ROUND(taxable × rate / 100, 2)
* tax documents split it:  intra-state  CGST = SGST = ROUND(taxable × rate / 200, 2)
                           inter-state  IGST = ROUND(taxable × rate / 100, 2)
* headers: sums of lines + a round-off of at most ±0.50 to a whole-rupee total.

``sgst`` holds UTGST when the place of supply is a union territory that uses it (1B ``states``);
the amount is the same, only the ledger account differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from scmgen.core.money import ZERO, MoneyError, dec, money, product, quantity, rate, round_half_up, sql_divide, total, unit_price, whole_rupees

MAX_GST_RATE = Decimal(50)
MAX_ROUND_OFF = Decimal("0.50")
# SQL scale of qty(3) * price(4) * (1 - disc(4) / 100 → scale 8): 3 + 4 + 8.
_INCLUSIVE_DIVIDEND_SCALE = 15
_PRICE_DIVIDEND_SCALE = 4


class GstError(ValueError):
    """Inputs that no document line may have."""


@dataclass(frozen=True)
class PricedLine:
    quantity: Decimal
    unit_price: Decimal                    # ex-GST, 4 decimals
    inclusive_unit_price: Decimal | None   # set only for GST-inclusive (B2C) pricing
    discount_percent: Decimal
    gst_rate: Decimal
    taxable_amount: Decimal


@dataclass(frozen=True)
class TaxSplit:
    cgst: Decimal
    sgst: Decimal
    igst: Decimal

    @property
    def total(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


@dataclass(frozen=True)
class TaxedLine:
    taxable_amount: Decimal
    gst_rate: Decimal
    split: TaxSplit

    @property
    def line_total(self) -> Decimal:
        return self.taxable_amount + self.split.total


@dataclass(frozen=True)
class DocumentTotals:
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    round_off_amount: Decimal
    total_amount: Decimal


def _checked_inputs(qty: Decimal | int | str, discount: Decimal | int | str, gst_rate: Decimal | int | str,
                    max_discount: Decimal | int) -> tuple[Decimal, Decimal, Decimal]:
    q, d, r = quantity(qty), rate(discount), rate(gst_rate)
    if q <= 0:
        raise GstError(f"Quantity must be positive, got {q}")
    if not 0 <= d <= dec(max_discount):
        raise GstError(f"Discount must be between 0 and {max_discount}%, got {d}")
    if not 0 <= r <= MAX_GST_RATE:
        raise GstError(f"GST rate must be between 0 and {MAX_GST_RATE}%, got {r}")
    return q, d, r


def price_exclusive(qty: Decimal | int | str, price: Decimal | int | str, discount_percent: Decimal | int | str,
                    gst_rate: Decimal | int | str, max_discount: Decimal | int = 100) -> PricedLine:
    """A line priced excluding GST (B2B sales, all purchase documents)."""
    q, d, r = _checked_inputs(qty, discount_percent, gst_rate, max_discount)
    p = unit_price(price)
    if p <= 0:
        raise GstError(f"Unit price must be positive, got {p}")
    taxable = money(product(q, p, 1 - d / 100))
    return PricedLine(q, p, None, d, r, taxable)


def price_inclusive(qty: Decimal | int | str, inclusive_price: Decimal | int | str,
                    discount_percent: Decimal | int | str, gst_rate: Decimal | int | str,
                    max_discount: Decimal | int = 90) -> PricedLine:
    """A line priced including GST (B2C shelf / MRP price, D79): taxable value is back-calculated."""
    q, d, r = _checked_inputs(qty, discount_percent, gst_rate, max_discount)
    incl = unit_price(inclusive_price)
    if incl <= 0:
        raise GstError(f"Inclusive price must be positive, got {incl}")
    divisor = 1 + r / 100
    ex_gst = round_half_up(sql_divide(incl, divisor, _PRICE_DIVIDEND_SCALE), 4)
    taxable = round_half_up(sql_divide(product(q, incl, 1 - d / 100), divisor, _INCLUSIVE_DIVIDEND_SCALE), 2)
    if ex_gst <= 0:
        raise GstError(f"Inclusive price {incl} is too small to carry {r}% GST")
    return PricedLine(q, ex_gst, incl, d, r, money(taxable))


def total_gst(taxable_amount: Decimal, gst_rate: Decimal | int | str) -> Decimal:
    """Total GST of an order line (orders are not tax documents, D39)."""
    return money(product(dec(taxable_amount), rate(gst_rate)) / 100)


def split_gst(taxable_amount: Decimal, gst_rate: Decimal | int | str, inter_state: bool) -> TaxSplit:
    """CGST + SGST (intra-state) or IGST (inter-state) on a tax-document line."""
    taxable, r = money(taxable_amount), rate(gst_rate)
    if taxable < 0:
        raise GstError(f"Taxable value can't be negative, got {taxable}")
    if inter_state:
        return TaxSplit(ZERO, ZERO, money(product(taxable, r) / 100))
    half = money(product(taxable, r) / 200)
    return TaxSplit(half, half, ZERO)


def tax_line(taxable_amount: Decimal, gst_rate: Decimal | int | str, inter_state: bool) -> TaxedLine:
    return TaxedLine(money(taxable_amount), rate(gst_rate), split_gst(taxable_amount, gst_rate, inter_state))


def document_totals(lines: Iterable[TaxedLine]) -> DocumentTotals:
    """Header totals: Σ lines, plus a round-off (±0.50) that makes the total a whole rupee."""
    lines = list(lines)
    if not lines:
        raise GstError("A document needs at least one line")
    taxable = total(line.taxable_amount for line in lines)
    cgst = total(line.split.cgst for line in lines)
    sgst = total(line.split.sgst for line in lines)
    igst = total(line.split.igst for line in lines)
    if igst and (cgst or sgst):
        raise GstError("A document is either intra-state (CGST+SGST) or inter-state (IGST), not both")
    exact = taxable + cgst + sgst + igst
    grand = whole_rupees(exact)
    round_off = grand - exact
    if abs(round_off) > MAX_ROUND_OFF:          # can't happen with half-up rounding; guard the invariant
        raise MoneyError(f"Round-off {round_off} exceeds ±{MAX_ROUND_OFF}")
    return DocumentTotals(taxable, cgst, sgst, igst, money(round_off), grand)


def is_inter_state(supplier_state_code: str, place_of_supply_state_code: str) -> bool:
    """GST place-of-supply rule: different states ⇒ IGST."""
    return supplier_state_code != place_of_supply_state_code


def shipping_charge_rate(goods_rates: Iterable[Decimal | int | str]) -> Decimal:
    """D42: the shipping line is taxed at the highest GST rate among the invoice's goods."""
    rates = [rate(value) for value in goods_rates]
    if not rates:
        raise GstError("A shipping charge needs at least one goods line on the invoice")
    return max(rates)


@dataclass(frozen=True)
class TransferTax:
    transfer_value: Decimal
    igst_amount: Decimal


def transfer_tax(quantity_shipped: Decimal | int | str, unit_cost: Decimal | int | str,
                 gst_rate: Decimal | int | str) -> TransferTax:
    """Inter-GSTIN stock transfer line (D29): value at MAC, IGST on it."""
    value = money(product(quantity(quantity_shipped), unit_price(unit_cost)))
    return TransferTax(value, money(product(value, rate(gst_rate)) / 100))


def net_unit_cost(taxable_amount: Decimal, line_quantity: Decimal | int | str) -> Decimal:
    """PO line net unit price used to value receipts (1C §4): ROUND(taxable / qty, 4) as MySQL computes it."""
    q = quantity(line_quantity)
    if q <= 0:
        raise GstError(f"Quantity must be positive, got {q}")
    return round_half_up(sql_divide(money(taxable_amount), q, 2), 4)
