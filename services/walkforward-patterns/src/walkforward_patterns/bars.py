"""Incremental M1 -> higher-timeframe bars, equal to `lib.resample(m1, rule)` (left-labelled, left-closed, empty
buckets dropped). Buckets are aligned on the research wall clock: 5/10/15M from :00, 1H on the hour, 4H at
00/04/08/12/16/20, and the trading day (17:00 -> 17:00, labelled by the date of t + 7h).
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import DAY


@dataclass(slots=True)
class Bar:
    start: int  # research minutes
    length: int  # minutes
    o: float
    h: float
    l: float
    c: float

    @property
    def end(self) -> int:
        return self.start + self.length


class Aggregator:
    """Builds one timeframe. `offset` shifts the bucket grid (the trading day uses +7h)."""

    def __init__(self, minutes: int, offset: int = 0):
        self.minutes = minutes
        self.offset = offset
        self.cur: Bar | None = None

    def bucket(self, t: int) -> int:
        m = self.minutes
        return (t + self.offset) // m * m - self.offset

    def flush_until(self, t: int) -> Bar | None:
        """Close the open bucket if it ends at or before research minute t."""
        cur = self.cur
        if cur is not None and cur.start + self.minutes <= t:
            self.cur = None
            return cur
        return None

    def add(self, t: int, o: float, h: float, l: float, c: float) -> Bar | None:
        """Add an M1 bar starting at t. Returns the bar this closes (if any)."""
        b = self.bucket(t)
        cur = self.cur
        closed = None
        if cur is not None and cur.start != b:
            closed = cur
            cur = None
        if cur is None:
            self.cur = Bar(b, self.minutes, o, h, l, c)
        else:
            if h > cur.h:
                cur.h = h
            if l < cur.l:
                cur.l = l
            cur.c = c
        return closed


def day_aggregator() -> Aggregator:
    """Trading-day bars; `Bar.start` is the 17:00 start, `trading_day` label = (start + 420) // DAY."""
    return Aggregator(DAY, offset=420)
