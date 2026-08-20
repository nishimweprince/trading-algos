"""Partial-trail: 50% at 1R, remainder to breakeven, runner at RR. Incumbent fixed_r unchanged."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from config import Settings
from engine import ClosedBarEngine
from models import Candle, EngineParams, Timeframe
from sessions import build_windows


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
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


def _engine(*, tp_mode: str = "partial_trail") -> ClosedBarEngine:
    return ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            pip_size=0.1,
            lock_pips=20,
            tp_mode=tp_mode,
            rr=3.0,
            partial_tp_r=1.0,
            partial_fraction=0.5,
            orb_minutes=15,
            timeframe_minutes=15,
            entry_delay_minutes=15,
            anchor_tolerance_minutes=15,
            intrabar_mode="optimistic",
        ),
    )


def _fill_pair(*, tp_mode: str = "partial_trail") -> ClosedBarEngine:
    """Range 2 → S=4. Fill both legs at 2000. Short stop and long 1R share entry+S."""
    engine = _engine(tp_mode=tp_mode)
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2000.1, low=1999.9, c=2000)
    )
    engine.step(_bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2002, low=2000, c=2001.5))
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.5, low=1999.8, c=2000.2)
    )
    return engine


def test_incumbent_fixed_r_still_targets_three_r() -> None:
    engine = _fill_pair(tp_mode="fixed_r")
    pair = engine.pairs[0]
    assert pair.long_tp == pytest.approx(pair.entry + 3 * pair.sl_dist)
    assert pair.short_tp == pytest.approx(pair.entry - 3 * pair.sl_dist)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.tp_mode == "fixed_r"


def test_partial_trail_first_target_is_one_r() -> None:
    engine = _fill_pair()
    pair = engine.pairs[0]
    assert pair.long_tp == pytest.approx(pair.entry + pair.sl_dist)
    assert pair.short_tp == pytest.approx(pair.entry - pair.sl_dist)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.tp_mode == "partial_trail"
    assert report.report_header.partial_tp_r == pytest.approx(1.0)
    assert report.report_header.partial_fraction == pytest.approx(0.5)


def test_partial_trail_lock_bar_scales_half_to_breakeven() -> None:
    engine = _fill_pair()
    pair = engine.pairs[0]
    entry = pair.entry
    three_r = entry + 3 * pair.sl_dist
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2001, h=2005, low=2000.5, c=2004.5)
    )
    assert pair.locked is True
    assert pair.short_open is False
    assert pair.long_open is True
    assert pair.long_partial_taken is True
    assert pair.long_qty == pytest.approx(0.5)
    assert pair.long_sl == pytest.approx(entry)
    assert pair.long_tp == pytest.approx(three_r)
    partials = [leg for leg in engine.trades if leg.reason == "partial_tp"]
    assert len(partials) == 1
    assert partials[0].qty == pytest.approx(0.5)
    assert partials[0].side == "long"
    assert any(event.kind == "partial_tp" for event in engine.events)


def test_partial_trail_runner_closes_the_remainder_at_three_r() -> None:
    engine = _fill_pair()
    pair = engine.pairs[0]
    three_r = pair.entry + 3 * pair.sl_dist
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2001, h=2005, low=2000.5, c=2004.5)
    )
    engine.step(
        _bar(
            datetime(2026, 1, 14, 14, 0, tzinfo=UTC),
            o=three_r - 0.2,
            h=three_r + 0.1,
            low=three_r - 0.4,
            c=three_r,
        )
    )
    assert pair.long_open is False
    runners = [leg for leg in engine.trades if leg.side == "long" and leg.reason == "sl_or_tp"]
    assert len(runners) == 1
    assert runners[0].qty == pytest.approx(0.5)
    assert runners[0].exit == pytest.approx(three_r)


def test_partial_trail_same_bar_runner_closes_the_remainder() -> None:
    engine = _fill_pair()
    pair = engine.pairs[0]
    three_r = pair.entry + 3 * pair.sl_dist
    engine.step(
        _bar(
            datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
            o=2001,
            h=three_r + 0.2,
            low=2000.5,
            c=three_r,
        )
    )
    assert pair.long_open is False
    long_legs = [leg for leg in engine.trades if leg.side == "long"]
    assert [leg.reason for leg in long_legs] == ["partial_tp", "sl_or_tp"]
    assert long_legs[0].qty == pytest.approx(0.5)
    assert long_legs[1].qty == pytest.approx(0.5)
    assert long_legs[1].exit == pytest.approx(three_r)


def test_partial_trail_same_bar_breakeven_closes_the_remainder() -> None:
    engine = _fill_pair()
    pair = engine.pairs[0]
    entry = pair.entry
    engine.step(
        _bar(
            datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
            o=2001,
            h=2005,
            low=entry - 0.4,
            c=entry - 0.2,
        )
    )
    assert pair.long_open is False
    long_legs = [leg for leg in engine.trades if leg.side == "long"]
    assert long_legs[0].reason == "partial_tp"
    assert long_legs[1].reason == "sl_or_tp"
    assert long_legs[1].exit == pytest.approx(entry)


def test_partial_trail_defaults_stay_off() -> None:
    params = Settings().engine_params()
    assert params.tp_mode == "fixed_r"
    assert params.partial_tp_r == pytest.approx(1.0)
    assert params.partial_fraction == pytest.approx(0.5)
    enabled = Settings(tp_mode="partial_trail").engine_params()
    assert enabled.tp_mode == "partial_trail"
