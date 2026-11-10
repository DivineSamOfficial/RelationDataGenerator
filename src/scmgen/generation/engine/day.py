"""One simulated business day (D64): the context every day step works in.

A day step receives a ``DayContext`` and writes through it. Inserts are buffered in the batch writer;
``update`` statements that depend on earlier rows of the same day flush the writer first, so the
database always sees cause before effect. The engine commits the whole day at once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scmgen.core.calendar import SimDay
from scmgen.core.rng import RandomStreams
from scmgen.generation.engine.world import World
from scmgen.generation.ids import IdAllocator
from scmgen.generation.master.context import MasterContext
from scmgen.generation.numbering import DocumentNumbers
from scmgen.generation.writer import BatchWriter


@dataclass
class DayContext:
    day: date
    conn: Connection
    world: World
    ids: IdAllocator
    writer: BatchWriter
    numbers: DocumentNumbers
    streams: RandomStreams
    run_id: int
    is_working_day: bool = True
    notes: dict[str, Any] = field(default_factory=dict)     # per-day facts steps share (e.g. calendar flags)

    @property
    def sim_day(self) -> SimDay:
        return SimDay(self.day)

    def at(self, hour: int, minute: int = 0, second: int = 0) -> datetime:
        return datetime.combine(self.day, time(hour, minute, second))

    def rng(self, name: str) -> random.Random:
        """The day's stream for ``name`` (D65): same seed + date + name ⇒ same draws, however runs are split."""
        return self.streams.stream(self.day, name)

    def master(self) -> MasterContext:
        """A master-data context stamped on this day, for builders shared with day 0 (launches, campaigns)."""
        world = self.world
        return MasterContext(config=world.config, seed=world.seed, packs=world.packs, streams=self.streams,
                             ids=self.ids, writer=self.writer, workspace_code=world.workspace_code,
                             company_name=world.company_name, setup_day=self.day, refs=world.refs)

    def execute(self, sql: str, params: dict[str, Any] | list[dict[str, Any]] | None = None) -> int:
        """Run an UPDATE/DELETE now, after flushing the rows buffered so far (cause before effect)."""
        self.writer.flush(self.conn)
        return self.conn.execute(text(sql), params or {}).rowcount

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        """Query after flushing the buffered rows, so a step sees what earlier steps did today."""
        self.writer.flush(self.conn)
        return [dict(r) for r in self.conn.execute(text(sql), params).mappings()]


def poisson(rng: random.Random, mean: float) -> int:
    """Number of events in a day with the given expected count (Knuth; fine for small means)."""
    if mean <= 0:
        return 0
    limit, count, product = pow(2.718281828459045, -mean), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count
