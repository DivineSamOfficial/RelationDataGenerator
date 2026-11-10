"""Journal building (D50, D61, D62) and account determination (D16).

``JournalBuilder`` collects debit and credit lines for one business event and refuses to produce an
entry unless Σ debits = Σ credits (D62): an unbalanced journal can never reach the database. Amounts
must already be exact to the paisa; the builder never rounds (a hidden rounding would hide a bug).

``AccountBook`` answers "which GL account?" from master data only (D16): singleton accounts by
``chart_of_accounts.system_role``, revenue / returns / COGS by business unit, GST by GSTIN ×
component × direction, expenses by category, cash by bank account. No account code appears in Python.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

from scmgen.core.money import ZERO, dec, money, total

# Every singleton role the posting rules use (1B §5); V-AC-04 checks each exists exactly once.
SYSTEM_ROLES = frozenset({
    "PAYMENT_CLEARING", "AR_CONTROL", "INVENTORY", "INVENTORY_IN_TRANSIT", "SUPPLIER_ADVANCES",
    "AP_CONTROL", "GRNI", "ACCRUED_EXPENSES", "CUSTOMER_ADVANCES", "SALARIES_PAYABLE",
    "SHARE_CAPITAL", "RETAINED_EARNINGS", "OPENING_BALANCE_EQUITY", "SHIPPING_INCOME", "ROUNDING_OFF",
    "INVENTORY_ADJUSTMENT_GAIN", "INVENTORY_WRITE_OFF", "PURCHASE_PRICE_VARIANCE", "FREIGHT_OUTWARD",
    "MARKETPLACE_COMMISSION", "BANK_CHARGES", "PAYMENT_GATEWAY_CHARGES", "INELIGIBLE_GST",
})
TAX_COMPONENTS = frozenset({"CGST", "SGST", "UTGST", "IGST"})
TAX_DIRECTIONS = frozenset({"INPUT", "OUTPUT"})


class JournalError(ValueError):
    """A journal that must not be posted."""


class UnbalancedJournalError(JournalError):
    """Σ debits ≠ Σ credits (D62)."""


@dataclass(frozen=True)
class JournalLine:
    line_number: int
    account_id: int
    debit_amount: Decimal
    credit_amount: Decimal
    cost_center_id: int | None = None
    customer_id: int | None = None
    supplier_id: int | None = None
    employee_id: int | None = None
    warehouse_id: int | None = None
    line_description: str | None = None


@dataclass(frozen=True)
class Journal:
    description: str
    lines: tuple[JournalLine, ...]

    @property
    def total_debit(self) -> Decimal:
        return total(line.debit_amount for line in self.lines)

    @property
    def total_credit(self) -> Decimal:
        return total(line.credit_amount for line in self.lines)

    def reversal(self, description: str) -> Journal:
        """Mirror image of this journal: every debit becomes a credit and vice versa (D61, P07/P09/P25)."""
        return Journal(description, tuple(
            replace(line, debit_amount=line.credit_amount, credit_amount=line.debit_amount) for line in self.lines))


@dataclass
class JournalBuilder:
    description: str
    _lines: list[dict] = field(default_factory=list)

    def debit(self, account_id: int, amount: Decimal, **dimensions: int | str | None) -> JournalBuilder:
        return self._add(account_id, dec(amount), True, dimensions)

    def credit(self, account_id: int, amount: Decimal, **dimensions: int | str | None) -> JournalBuilder:
        return self._add(account_id, dec(amount), False, dimensions)

    def _add(self, account_id: int, amount: Decimal, is_debit: bool, dimensions: Mapping) -> JournalBuilder:
        if money(amount) != amount:
            raise JournalError(f"Amount {amount} isn't rounded to the paisa; round it where it is calculated")
        unknown = set(dimensions) - {"cost_center_id", "customer_id", "supplier_id", "employee_id", "warehouse_id",
                                     "line_description"}
        if unknown:
            raise JournalError(f"Unknown journal line field(s): {', '.join(sorted(unknown))}")
        parties = [k for k in ("customer_id", "supplier_id", "employee_id") if dimensions.get(k) is not None]
        if len(parties) > 1:
            raise JournalError(f"A journal line names at most one party, got {parties}")
        if amount == 0:
            return self                         # e.g. IGST on an intra-state invoice: no line
        if amount < 0:                          # a negative debit is a credit (e.g. a favourable PPV)
            amount, is_debit = -amount, not is_debit
        self._lines.append({"account_id": account_id,
                            "debit_amount": amount if is_debit else ZERO,
                            "credit_amount": ZERO if is_debit else amount, **dimensions})
        return self

    def build(self) -> Journal:
        if len(self._lines) < 2:
            raise JournalError(f"A journal needs at least two lines, got {len(self._lines)}: {self.description}")
        lines = tuple(JournalLine(line_number=i, **line) for i, line in enumerate(self._lines, 1))
        journal = Journal(self.description, lines)
        if journal.total_debit != journal.total_credit:
            raise UnbalancedJournalError(
                f"Unbalanced journal '{self.description}': debits {journal.total_debit} ≠ credits {journal.total_credit}")
        return journal


@dataclass(frozen=True)
class BusinessUnitAccounts:
    revenue_account_id: int
    sales_returns_account_id: int
    cogs_account_id: int


@dataclass(frozen=True)
class AccountBook:
    """GL account lookup built from master data (loaded from the workspace in a later step)."""

    roles: Mapping[str, int]
    business_units: Mapping[int, BusinessUnitAccounts] = field(default_factory=dict)
    tax_accounts: Mapping[tuple[int, str, str], int] = field(default_factory=dict)   # (gstin id, component, direction)
    expense_categories: Mapping[int, int] = field(default_factory=dict)
    bank_accounts: Mapping[int, int] = field(default_factory=dict)

    def missing_roles(self) -> set[str]:
        return set(SYSTEM_ROLES - set(self.roles))

    def role(self, name: str) -> int:
        if name not in SYSTEM_ROLES:
            raise JournalError(f"Unknown system role {name!r}")
        try:
            return self.roles[name]
        except KeyError:
            raise JournalError(f"No account carries system_role {name} (V-AC-04)") from None

    def _bu(self, business_unit_id: int) -> BusinessUnitAccounts:
        try:
            return self.business_units[business_unit_id]
        except KeyError:
            raise JournalError(f"Unknown business unit {business_unit_id}") from None

    def revenue(self, business_unit_id: int) -> int:
        return self._bu(business_unit_id).revenue_account_id

    def sales_returns(self, business_unit_id: int) -> int:
        return self._bu(business_unit_id).sales_returns_account_id

    def cogs(self, business_unit_id: int) -> int:
        return self._bu(business_unit_id).cogs_account_id

    def tax(self, company_gst_registration_id: int, component: str, direction: str) -> int:
        if component not in TAX_COMPONENTS or direction not in TAX_DIRECTIONS:
            raise JournalError(f"Unknown GST component/direction {component}/{direction}")
        try:
            return self.tax_accounts[(company_gst_registration_id, component, direction)]
        except KeyError:
            raise JournalError(f"No {direction} {component} account for GSTIN {company_gst_registration_id}") from None

    def expense(self, expense_category_id: int) -> int:
        try:
            return self.expense_categories[expense_category_id]
        except KeyError:
            raise JournalError(f"Unknown expense category {expense_category_id}") from None

    def bank(self, bank_account_id: int) -> int:
        try:
            return self.bank_accounts[bank_account_id]
        except KeyError:
            raise JournalError(f"Unknown bank account {bank_account_id}") from None
