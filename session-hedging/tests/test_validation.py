from __future__ import annotations

from datetime import UTC, datetime

from engine import ClosedBarEngine
from models import Candle, EngineParams
from sessions import build_windows
from validation import (
    DUPLICATE_TS,
    GAP,
    INVERTED_OHLC,
    NON_MONOTONIC,
    WRONG_INTERVAL,
    validate_bar,
)


def _bar(
    ts: datetime, *, o: float = 2000, h: float = 2010, low: float = 1990, c: float = 2005
) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_inverted_ohlc_is_rejected() -> None:
    bar = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=1990, low=2010, c=2005)
    rejection = validate_bar(bar, None, 15)
    assert rejection is not None
    assert rejection.reason == INVERTED_OHLC


def test_duplicate_timestamp_is_rejected() -> None:
    ts = datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    prev = _bar(ts)
    rejection = validate_bar(_bar(ts, o=2001), prev, 15)
    assert rejection is not None
    assert rejection.reason == DUPLICATE_TS


def test_non_monotonic_timestamp_is_rejected() -> None:
    prev = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC))
    earlier = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC))
    rejection = validate_bar(earlier, prev, 15)
    assert rejection is not None
    assert rejection.reason == NON_MONOTONIC


def test_wrong_interval_is_rejected() -> None:
    prev = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC))
    odd = _bar(datetime(2026, 1, 14, 13, 22, tzinfo=UTC))
    rejection = validate_bar(odd, prev, 15)
    assert rejection is not None
    assert rejection.reason == WRONG_INTERVAL


def test_gap_beyond_policy_is_rejected() -> None:
    prev = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC))
    later = _bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC))  # 3 missing M15 bars
    rejection = validate_bar(later, prev, 15)
    assert rejection is not None
    assert rejection.reason == GAP


def test_weekend_gap_is_allowed() -> None:
    friday = _bar(datetime(2026, 1, 16, 21, 0, tzinfo=UTC))
    sunday = _bar(datetime(2026, 1, 18, 22, 0, tzinfo=UTC))
    assert validate_bar(sunday, friday, 15) is None


def test_engine_emits_gap_but_still_processes_the_bar() -> None:
    params = EngineParams(orb_minutes=15, entry_delay_minutes=15, timeframe_minutes=15)
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008)
    engine.step(pre)
    engine.step(signal)
    fill = _bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010)
    events = engine.step(fill)
    assert any(
        event.kind == "bar_skipped_invalid" and event.detail["reason"] == GAP for event in events
    )
    assert len(engine.pairs) == 1
    assert engine.pairs[0].entry == 2009.0


def test_engine_skips_bad_bar_and_does_not_fill() -> None:
    params = EngineParams(orb_minutes=15, entry_delay_minutes=15, timeframe_minutes=15)
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008)
    engine.step(pre)
    engine.step(signal)
    assert "new_york" in engine.pending
    inverted = _bar(
        datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2000, low=2011, c=2010
    )
    events = engine.step(inverted)
    assert events[0].kind == "bar_skipped_invalid"
    assert events[0].detail["reason"] == INVERTED_OHLC
    assert engine.pairs == []
    assert "new_york" in engine.pending
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010)
    engine.step(fill)
    assert len(engine.pairs) == 1
    assert engine.pairs[0].entry == 2009.0
