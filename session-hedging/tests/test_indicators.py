"""Wilder ATR14 and the frozen 50/50 opening-range blend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from indicators import ATR14_PERIOD, blended_orb_atr, true_range, wilder_atr
from models import Candle


def _bar(i: int, *, high: float, low: float, close: float, open_: float | None = None) -> Candle:
    ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=15 * (i + 1))
    open_px = close if open_ is None else open_
    return Candle(
        ts=ts,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=1,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_true_range_uses_prior_close_gaps() -> None:
    bar = _bar(1, high=12, low=8, close=9, open_=10)
    assert true_range(bar, prev_close=5) == 7.0  # high - prev_close


def test_wilder_atr14_needs_fifteen_bars_then_matches_constant_tr() -> None:
    bars = [_bar(i, high=12, low=10, close=10) for i in range(ATR14_PERIOD)]
    assert wilder_atr(bars) is None
    bars.append(_bar(ATR14_PERIOD, high=12, low=10, close=10))
    assert wilder_atr(bars) == 2.0
    bars.append(_bar(ATR14_PERIOD + 1, high=16, low=10, close=10))
    expected = (2.0 * 13 + 6.0) / 14
    assert wilder_atr(bars) == expected


def test_blend_is_frozen_fifty_fifty() -> None:
    assert blended_orb_atr(10.0, 2.0) == 6.0
