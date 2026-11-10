"""Simulated time: business days, financial years and timestamps (D7, D67, D77).

All timestamps are naive ``datetime`` values in IST (Asia/Kolkata, D77). The engine simulates one day
at a time; every row recorded during day D gets a timestamp inside D, so audit timestamps only move
forward from day to day (D67) and ``committed_through`` = the last microsecond of the last committed day.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
END_OF_DAY = time(23, 59, 59, 999999)


def today_ist() -> date:
    """The real calendar date in India now (wall clock), used for "up to today" defaults (D68)."""
    return datetime.now(IST).date()


def wall_clock() -> datetime:
    """Naive IST wall-clock time, for system tables' timing columns (D74)."""
    return datetime.now(IST).replace(tzinfo=None)


def fiscal_year(day: date, start_month: int) -> int:
    """Calendar year in which the financial year containing ``day`` starts (April start: 2027-01-15 → 2026)."""
    if not 1 <= start_month <= 12:
        raise ValueError(f"start_month must be 1-12, got {start_month}")
    return day.year if day.month >= start_month else day.year - 1


def fiscal_year_label(year: int, start_month: int = 4) -> str:
    """Readable FY label used in document numbers: ``2026-27`` (``2026`` when the FY is a calendar year)."""
    return str(year) if start_month == 1 else f"{year}-{(year + 1) % 100:02d}"


def fiscal_year_compact(year: int) -> str:
    """Four-digit FY used in GST tax-document numbers (D38): 2026-27 → ``2627``."""
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def days(start: date, end: date) -> Iterator[date]:
    """Every date from ``start`` to ``end`` inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


@dataclass(frozen=True)
class SimDay:
    """One simulated business day and the timestamps that may be recorded in it."""

    day: date

    @property
    def start(self) -> datetime:
        return datetime.combine(self.day, time.min)

    @property
    def end(self) -> datetime:
        """Last microsecond of the day: the value of ``committed_through`` once the day commits (D67)."""
        return datetime.combine(self.day, END_OF_DAY)

    def at(self, hour: int, minute: int = 0, second: int = 0, microsecond: int = 0) -> datetime:
        return datetime.combine(self.day, time(hour, minute, second, microsecond))

    def random_time(self, rng: random.Random, from_hour: int = 0, to_hour: int = 24) -> datetime:
        """A timestamp in [from_hour:00, to_hour:00) of this day, to the second (e.g. dispatches 10-19)."""
        if not 0 <= from_hour < to_hour <= 24:
            raise ValueError(f"Invalid hour window {from_hour}-{to_hour}")
        seconds = rng.randrange(from_hour * 3600, to_hour * 3600)
        return self.start + timedelta(seconds=seconds)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end
