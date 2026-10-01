"""Assertions every market-data plugin's tests share.

They pin the parts of the contract a provider can get subtly wrong and a
consumer cannot detect: bar timestamps, ordering, and the forming bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from ta_contracts import TIMEFRAME_MINUTES, Candle, Timeframe

__all__ = ["assert_closed_utc_candles"]


def assert_closed_utc_candles(
    candles: Sequence[Candle], timeframe: Timeframe, *, now: datetime
) -> None:
    """Oldest first, no duplicates, aligned interval ends, none still forming."""
    step = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    stamps = [candle.ts for candle in candles]
    assert stamps == sorted(set(stamps)), "candles must be strictly ascending"
    for ts in stamps:
        assert ts.utcoffset() == timedelta(0), f"{ts} is not UTC"
        assert ts <= now, f"{ts} ends after {now}: the forming bar leaked"
        if step <= timedelta(days=1):
            midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            assert (ts - midnight) % step == timedelta(0), f"{ts} is not an interval end"
