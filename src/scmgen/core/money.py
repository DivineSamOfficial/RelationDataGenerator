"""Exact decimal arithmetic for money, prices, quantities and rates (D84).

MySQL silently rounds excess DECIMAL digits, and the DDL's formula CHECKs compare amounts with MySQL's
``ROUND`` (half away from zero). So every amount the engine writes must already be quantized, the
same way, before the INSERT. Python's ``ROUND_HALF_UP`` is that rule (proven in test A10/A11).

Floats are refused everywhere: ``0.1 + 0.2`` is not ``0.3`` in binary floating point, and one stray
float would make a tax line a paisa off and the database would reject it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

# Column scales from the database standards (CLAUDE.md).
MONEY_PLACES = 2        # DECIMAL(18,2)
UNIT_PLACES = 4         # DECIMAL(18,4) unit prices and costs
QTY_PLACES = 3          # DECIMAL(18,3)
RATE_PLACES = 4         # DECIMAL(7,4)

_DECIMAL_18_LIMIT = {places: Decimal(10) ** (18 - places) for places in range(0, 7)}
_RATE_LIMIT = Decimal(1000)                       # DECIMAL(7,4) holds up to 999.9999
_EXPONENT = {places: Decimal(1).scaleb(-places) for places in range(0, 7)}

ZERO = Decimal("0")


class MoneyError(ValueError):
    """A value can't be used as an exact database amount."""


def dec(value: Decimal | int | str) -> Decimal:
    """Convert to Decimal, refusing floats (and bools, which are ints in Python)."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyError(f"Not a finite number: {value}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MoneyError(f"Use Decimal, int or str for amounts, not {type(value).__name__} ({value!r})")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise MoneyError(f"Not a number: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError(f"Not a finite number: {value!r}")
    return result


def round_half_up(value: Decimal | int | str, places: int) -> Decimal:
    """Round like MySQL ``ROUND(value, places)`` on DECIMAL: halves go away from zero."""
    return dec(value).quantize(_EXPONENT[places], rounding=ROUND_HALF_UP)


def _checked(value: Decimal, places: int) -> Decimal:
    if abs(value) >= _DECIMAL_18_LIMIT[places]:
        raise MoneyError(f"{value} does not fit DECIMAL(18,{places})")
    return value


def money(value: Decimal | int | str) -> Decimal:
    """Quantize to DECIMAL(18,2)."""
    return _checked(round_half_up(value, MONEY_PLACES), MONEY_PLACES)


def unit_price(value: Decimal | int | str) -> Decimal:
    """Quantize to DECIMAL(18,4) (unit prices and unit costs)."""
    return _checked(round_half_up(value, UNIT_PLACES), UNIT_PLACES)


def quantity(value: Decimal | int | str) -> Decimal:
    """Quantize to DECIMAL(18,3)."""
    return _checked(round_half_up(value, QTY_PLACES), QTY_PLACES)


def rate(value: Decimal | int | str) -> Decimal:
    """Quantize to DECIMAL(7,4) (GST rates, discount percentages)."""
    result = round_half_up(value, RATE_PLACES)
    if abs(result) >= _RATE_LIMIT:
        raise MoneyError(f"{result} does not fit DECIMAL(7,4)")
    return result


def whole_rupees(value: Decimal | int | str) -> Decimal:
    """Round to the whole rupee (invoice totals), kept at two places: ``123.50`` → ``124.00``."""
    return money(round_half_up(value, 0))


def total(values: Iterable[Decimal]) -> Decimal:
    """Exact sum of already-quantized amounts (refuses floats)."""
    return sum((dec(v) for v in values), ZERO)
