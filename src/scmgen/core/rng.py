"""Deterministic random streams (D65).

Every random choice draws from a stream keyed by **(workspace base seed, simulated date, stream name)**,
for example ``(42, 2025-10-21, "orders")``. A stream depends on nothing else: not on how many days ran
before it in this process, nor on which other streams were used. So one 2-year run, 730 one-day
Continue runs, or a crash and resume all produce identical data.

The seed of a stream is the first 8 bytes of SHA-256 over the key, so neighbouring dates or similar
names give unrelated sequences.

Reproducibility holds for the same Python minor version and the same Faker version: Python only
guarantees ``random()`` across versions, and Faker's word lists change between releases (Faker is
pinned exactly). Both versions are recorded in every run's config snapshot.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date

from faker import Faker

FAKER_LOCALE = "en_IN"
_STREAM_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


def stream_seed(base_seed: int, day: date, name: str) -> int:
    """The 64-bit seed of stream ``name`` on ``day``."""
    if not _STREAM_NAME.match(name):
        raise ValueError(f"Invalid stream name {name!r}: use lower-case words separated by dots, e.g. 'orders.b2c'")
    if base_seed < 0:
        raise ValueError("base_seed must not be negative")
    key = f"{base_seed}|{day.isoformat()}|{name}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


class RandomStreams:
    """Factory for the random streams of one workspace."""

    def __init__(self, base_seed: int) -> None:
        self.base_seed = base_seed
        self._faker: Faker | None = None

    def stream(self, day: date, name: str) -> random.Random:
        """A fresh, independent generator for ``name`` on ``day``. Same arguments ⇒ same sequence."""
        return random.Random(stream_seed(self.base_seed, day, name))

    def faker(self, day: date, name: str) -> Faker:
        """An ``en_IN`` Faker seeded for ``name`` on ``day`` (master-data attributes only).

        One Faker object is reused and reseeded on each call (creating one is slow), so use the returned
        object before asking for the next stream.
        """
        if self._faker is None:
            self._faker = Faker(FAKER_LOCALE)
        self._faker.seed_instance(stream_seed(self.base_seed, day, name))
        return self._faker
