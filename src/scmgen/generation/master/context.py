"""Shared state of a master-data build (1B §3).

Master data is created on **day 0, the setup day = the day before the first simulated day**: every row
is stamped then (D21/D67), so the historical simulation can start the next day with all master data
committed before it. Business dates (incorporation, hire dates, launch dates) may lie years earlier.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from faker import Faker

from scmgen.core.rng import RandomStreams
from scmgen.core.errors import ConfigError
from scmgen.generation.config import GenerationConfig
from scmgen.generation.ids import IdAllocator
from scmgen.generation.packs import IndustryPack
from scmgen.generation.seed import SeedData
from scmgen.generation.writer import BatchWriter

SETUP_TIME = time(9, 0)          # master data is recorded on the morning of the setup day


class MasterDataError(ConfigError):
    """Setup inputs that can't produce valid master data (unknown city, HQ without a warehouse …)."""


@dataclass
class MasterContext:
    config: GenerationConfig
    seed: SeedData
    packs: list[IndustryPack]
    streams: RandomStreams
    ids: IdAllocator
    writer: BatchWriter
    workspace_code: str
    company_name: str
    setup_day: date
    # Lookups filled by the builders as they go: refs["state"]["KA"] -> state_id, etc.
    refs: dict[str, dict[Any, Any]] = field(default_factory=dict)

    @property
    def stamp(self) -> datetime:
        return datetime.combine(self.setup_day, SETUP_TIME)

    def rng(self, name: str) -> random.Random:
        return self.streams.stream(self.setup_day, f"master.{name}")

    def faker(self, name: str) -> Faker:
        return self.streams.faker(self.setup_day, f"master.{name}")

    def add(self, table: str, row: dict[str, Any]) -> None:
        """Buffer a row with the setup-day audit timestamps."""
        self.writer.insert(table, {**row, "created_at": self.stamp, "updated_at": self.stamp})

    def ref(self, kind: str) -> dict[Any, Any]:
        return self.refs.setdefault(kind, {})

    def city(self, name: str) -> dict[str, Any]:
        matches = [c for c in self.seed.cities if c["city"] == name]
        if len(matches) != 1:
            raise MasterDataError(f"City {name!r} must appear exactly once in config/seed/cities.yaml")
        return matches[0]


def city_code(name: str, taken: set[str]) -> str:
    """Three-letter code for a city: first letter + next consonants (Bengaluru → BNG, Mumbai → MMB)."""
    letters = re.sub(r"[^A-Z]", "", name.upper())
    consonants = [c for c in letters[1:] if c not in "AEIOU"]
    base = (letters[0] + "".join(consonants))[:3].ljust(3, "X")
    code, n = base, 1
    while code in taken:
        n += 1
        code = f"{base[:2]}{n}"
    taken.add(code)
    return code


def domain_slug(name: str) -> str:
    """Web/email domain stem from a trade name: 'Kaveri Nexus' → 'kaveri-nexus'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def trade_name_of(legal_name: str) -> str:
    """Legal name without its company-form suffix: 'Kaveri Nexus Distribution Pvt. Ltd.' → 'Kaveri Nexus Distribution'."""
    stripped = re.sub(r"[\s,]+(private limited|pvt\.? ?ltd\.?|limited|ltd\.?|llp)\s*$", "", legal_name.strip(), flags=re.I)
    return stripped or legal_name.strip()
