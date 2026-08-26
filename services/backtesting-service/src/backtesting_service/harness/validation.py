"""Candle quality gates. A bad bar must not become a fill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import Candle

GAP_MAX_BARS = 2

INVERTED_OHLC = "inverted_ohlc"
DUPLICATE_TS = "duplicate_ts"
NON_MONOTONIC = "non_monotonic"
GAP = "gap"
WRONG_INTERVAL = "wrong_interval"


@dataclass(frozen=True, slots=True)
class BarRejection:
    reason: str
    detail: dict[str, object]


def ohlc_sane(bar: Candle) -> bool:
    return bar.low <= min(bar.open, bar.close) and max(bar.open, bar.close) <= bar.high


def weekend_or_close_gap(prev: datetime, nxt: datetime) -> bool:
    """True when the span crosses Saturday or is a multi-day close."""
    if nxt <= prev:
        return False
    if nxt - prev >= timedelta(hours=36):
        return True
    cursor = prev
    while cursor < nxt:
        if cursor.weekday() >= 5:
            return True
        cursor += timedelta(hours=1)
    return False


def validate_bar(
    bar: Candle,
    prev: Candle | None,
    timeframe_minutes: int,
    *,
    max_gap_bars: int = GAP_MAX_BARS,
) -> BarRejection | None:
    if not ohlc_sane(bar):
        return BarRejection(
            INVERTED_OHLC,
            {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close},
        )
    if prev is None:
        return None
    if bar.ts == prev.ts:
        return BarRejection(DUPLICATE_TS, {"ts": bar.ts.isoformat()})
    if bar.ts < prev.ts:
        return BarRejection(
            NON_MONOTONIC,
            {"prev_ts": prev.ts.isoformat(), "ts": bar.ts.isoformat()},
        )
    delta_minutes = (bar.ts - prev.ts).total_seconds() / 60.0
    if weekend_or_close_gap(prev.ts, bar.ts):
        return None
    if delta_minutes <= 0:
        return BarRejection(NON_MONOTONIC, {"delta_minutes": delta_minutes})
    remainder = delta_minutes % timeframe_minutes
    if remainder > 1e-6 and abs(remainder - timeframe_minutes) > 1e-6:
        return BarRejection(
            WRONG_INTERVAL,
            {"delta_minutes": delta_minutes, "expected_minutes": timeframe_minutes},
        )
    missing = delta_minutes / timeframe_minutes - 1.0
    if missing > max_gap_bars + 1e-9:
        return BarRejection(
            GAP,
            {
                "delta_minutes": delta_minutes,
                "missing_bars": missing,
                "max_gap_bars": max_gap_bars,
            },
        )
    return None
