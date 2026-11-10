"""Fictional Indian business identifiers (1B §2.3, D24, D36, D46).

Every value matches its DDL format CHECK. Identifiers that could collide with real registrations are
made recognisably synthetic:

* every PAN starts with ``ZZX`` (D36), and GSTINs are built from those PANs, so positions 3-5 of every
  GSTIN read ``ZZX``. GSTINs are otherwise fully valid, including the check digit;
* bank IFSC prefixes and bank names are invented (D24, seed data), emails use the ``.example`` domain.

All functions take the caller's random stream, so values are reproducible (D65). Uniqueness across
rows is the caller's job (master-data generation keeps a set per identifier).
"""

from __future__ import annotations

import hashlib
import random
import re
import string

GSTIN_CHARSET = string.digits + string.ascii_uppercase
SYNTHETIC_PAN_PREFIX = "ZZX"
# PAN 4th character (holder type): P person, C company, F firm/LLP, H HUF, A AOP, T trust, G government.
PAN_HOLDER_TYPES = frozenset("PCFHATG")

GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CIN_PATTERN = re.compile(r"^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PIN_PATTERN = re.compile(r"^[1-9][0-9]{5}$")
MOBILE_PATTERN = re.compile(r"^[6-9][0-9]{9}$")
VEHICLE_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z]{1,3}[0-9]{4}$")


class IdentifierError(ValueError):
    """Inputs that can't form a valid identifier."""


def _initial(name: str) -> str:
    letters = [c for c in name.upper() if c in string.ascii_uppercase]
    if not letters:
        raise IdentifierError(f"Name {name!r} has no letter to use as the PAN initial")
    return letters[0]


def pan(rng: random.Random, holder_type: str, name: str, prefix: str = SYNTHETIC_PAN_PREFIX) -> str:
    """``ZZX`` + holder type + name initial + 4 digits + letter, e.g. ``ZZXCK4821M``.

    For people the initial is the surname's; for organisations, the name's first letter.
    """
    if holder_type not in PAN_HOLDER_TYPES:
        raise IdentifierError(f"Unknown PAN holder type {holder_type!r}")
    if not re.fullmatch(r"[A-Z]{3}", prefix):
        raise IdentifierError(f"PAN prefix must be three capital letters, got {prefix!r}")
    return f"{prefix}{holder_type}{_initial(name)}{rng.randint(1, 9999):04d}{rng.choice(string.ascii_uppercase)}"


def gstin_check_character(first14: str) -> str:
    """GSTIN check character: Luhn mod 36 over the first 14 characters (factors 1, 2, 1, 2 …)."""
    if len(first14) != 14 or any(c not in GSTIN_CHARSET for c in first14):
        raise IdentifierError(f"Need 14 characters from 0-9A-Z, got {first14!r}")
    total = 0
    for position, char in enumerate(first14):
        value = GSTIN_CHARSET.index(char) * (2 if position % 2 else 1)
        total += value // 36 + value % 36
    return GSTIN_CHARSET[(36 - total % 36) % 36]


def gstin(gst_state_code: str, holder_pan: str, entity_number: int = 1) -> str:
    """State code + PAN + registration number in that state (1-9, then A-Z) + ``Z`` + check character."""
    if not re.fullmatch(r"[0-9]{2}", gst_state_code):
        raise IdentifierError(f"GST state code must be two digits, got {gst_state_code!r}")
    if not PAN_PATTERN.match(holder_pan):
        raise IdentifierError(f"Invalid PAN {holder_pan!r}")
    if not 1 <= entity_number <= 35:
        raise IdentifierError(f"Entity number must be 1-35, got {entity_number}")
    first14 = f"{gst_state_code}{holder_pan}{GSTIN_CHARSET[entity_number]}Z"
    return first14 + gstin_check_character(first14)


def is_valid_gstin(value: str) -> bool:
    """Format and check character both valid (validation V-MD-02)."""
    return bool(GSTIN_PATTERN.match(value)) and gstin_check_character(value[:14]) == value[14]


def cin(rng: random.Random, state_code: str, incorporation_year: int, listed: bool = False,
        industry_code: str | None = None) -> str:
    """Corporate identity number: U/L + NIC industry code + state + year + PTC/PLC + 6 digits."""
    if not re.fullmatch(r"[A-Z]{2}", state_code):
        raise IdentifierError(f"State code must be two capital letters, got {state_code!r}")
    nic = industry_code or "46909"                    # NIC 2008: wholesale trade, n.e.c.
    if not re.fullmatch(r"[0-9]{5}", nic) or not 1900 <= incorporation_year <= 2100:
        raise IdentifierError("Invalid industry code or incorporation year")
    return f"{'L' if listed else 'U'}{nic}{state_code}{incorporation_year}{'PLC' if listed else 'PTC'}{rng.randint(1, 999999):06d}"


def ifsc(rng: random.Random, bank_code: str) -> str:
    """Bank code (4 letters, invented in seed data, D24) + ``0`` + 6-character branch code."""
    if not re.fullmatch(r"[A-Z]{4}", bank_code):
        raise IdentifierError(f"Bank code must be four capital letters, got {bank_code!r}")
    return f"{bank_code}0{rng.randint(0, 999999):06d}"


def pincode(rng: random.Random, prefix: str, local_max: int | None = None) -> str:
    """A 6-digit PIN starting with the area's prefix (1-3 digits, from seed data).

    ``local_max`` limits the digits after the prefix: city PINs mostly sit low in their range
    (Bengaluru 560001-560100…), so city addresses use a small value.
    """
    if not re.fullmatch(r"[1-9][0-9]{0,2}", prefix):
        raise IdentifierError(f"PIN prefix must be 1-3 digits not starting with 0, got {prefix!r}")
    rest = 6 - len(prefix)
    high = min(local_max, 10 ** rest - 1) if local_max else 10 ** rest - 1
    return prefix + f"{rng.randint(1 if local_max else 0, high):0{rest}d}"


def mobile(rng: random.Random) -> str:
    """10-digit Indian mobile number without +91 (starts with 6-9)."""
    return f"{rng.randint(6, 9)}{rng.randint(0, 999_999_999):09d}"


def email(local_part: str, domain: str) -> str:
    """``local@domain.example`` (D24). Both parts are reduced to letters, digits, dots and hyphens."""
    local = re.sub(r"[^a-z0-9.]+", ".", local_part.lower()).strip(".")
    host = re.sub(r"[^a-z0-9-]+", "-", domain.lower()).strip("-")
    if not local or not host:
        raise IdentifierError(f"Can't build an email from {local_part!r} and {domain!r}")
    return f"{local}@{host}.example"


def irn(supplier_gstin: str, fiscal_year_label: str, document_type: str, document_number: str) -> str:
    """e-invoice Invoice Reference Number (D46): SHA-256 of GSTIN + FY + document type + number, hex."""
    key = f"{supplier_gstin}{fiscal_year_label}{document_type}{document_number}"
    return hashlib.sha256(key.encode()).hexdigest()


def eway_bill_number(rng: random.Random) -> str:
    """12-digit e-way bill number."""
    return f"{rng.randint(1, 9)}{rng.randint(0, 10 ** 11 - 1):011d}"


def vehicle_number(rng: random.Random, state_code: str) -> str:
    """Registration such as ``KA01AB1234``: state + RTO district + series + number."""
    if not re.fullmatch(r"[A-Z]{2}", state_code):
        raise IdentifierError(f"State code must be two capital letters, got {state_code!r}")
    series = "".join(rng.choice(string.ascii_uppercase) for _ in range(rng.choice((1, 2, 2, 3))))
    return f"{state_code}{rng.randint(1, 99):02d}{series}{rng.randint(1, 9999):04d}"


def tracking_number(carrier_prefix: str, sequence: int) -> str:
    """Carrier AWB number: the carrier's prefix + a zero-padded running number (unique per carrier)."""
    if not re.fullmatch(r"[A-Z]{2,6}", carrier_prefix) or sequence < 1:
        raise IdentifierError("Invalid carrier prefix or sequence")
    return f"{carrier_prefix}{sequence:010d}"
