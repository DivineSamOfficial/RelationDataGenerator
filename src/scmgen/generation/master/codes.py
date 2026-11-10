"""Formats of the codes master-data generation assigns (checked by V-MD-01).

The DDL only constrains a broad shape (e.g. ``^[A-Z0-9][A-Z0-9-]{2,29}$`` for a SKU); these are the
generator's own, stricter conventions, so a hand-edited or anomalous code stands out in validation.
"""

from __future__ import annotations

CODE_PATTERNS: dict[tuple[str, str], str] = {
    ("warehouses", "warehouse_code"): r"^(DC-[A-Z0-9]{3}|ST-[A-Z0-9]{3}-[0-9]{2})$",
    ("warehouse_locations", "location_code"): r"^((RCV|QRN|DEF|RET|PCK|DSP)-[0-9]{2}|(STG|PIK|SFL)-A[0-9]{2}-R[0-9]{2}-L[0-9])$",
    ("employees", "employee_code"): r"^EMP-[0-9]{5}$",
    ("bank_accounts", "account_code"): r"^(BNK-(COLL|PAY|CC)-[0-9]{2}|PETTY-(DC|ST)-[A-Z0-9-]+)$",
    ("cost_centers", "cost_center_code"): r"^(CORP|BU-[A-Z0-9]+|WH-(DC|ST)-[A-Z0-9-]+|DEPT-[A-Z]+)$",
    ("suppliers", "supplier_code"): r"^SUP-[0-9]{5}$",
    ("customers", "customer_code"): r"^CUST-[0-9]{6}$",
    ("products", "sku"): r"^[A-Z][A-Z0-9]{1,9}-[A-Z0-9]{2,4}-[0-9]{5}$",
    ("products", "style_code"): r"^[A-Z][A-Z0-9]{1,9}-S[0-9]{5}$",
    ("marketing_campaigns", "campaign_code"): r"^CMP-[0-9]{4}-[0-9]{3}$",
}


def supplier_code(number: int) -> str:
    return f"SUP-{number:05d}"


def customer_code(number: int) -> str:
    return f"CUST-{number:06d}"


def sku(business_unit_code: str, category_code: str, number: int) -> str:
    return f"{business_unit_code}-{category_code.split('-')[-1][:4]}-{number:05d}"


def style_code(business_unit_code: str, number: int) -> str:
    return f"{business_unit_code}-S{number:05d}"


def campaign_code(year: int, number: int) -> str:
    return f"CMP-{year}-{number:03d}"
