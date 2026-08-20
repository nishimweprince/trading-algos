"""ATR14 and 50/50 ORB–ATR14 stop estimators. Incumbent default remains bar_range."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config import Settings
from engine import ClosedBarEngine
from indicators import ATR14_PERIOD, blended_orb_atr, wilder_atr
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


def _engine(stop_mode: str = "atr14", sl_mult: float = 2.0) -> ClosedBarEngine:
    return ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            stop_mode=stop_mode,
            sl_mult=sl_mult,
            orb_minutes=15,
            timeframe_minutes=15,
            entry_delay_minutes=15,
            anchor_tolerance_minutes=15,
        ),
    )


def _warmup_constant_tr(engine: ClosedBarEngine, n: int) -> None:
    start = datetime(2026, 1, 14, 8, 0, tzinfo=UTC)
    px = 1990.0
    for i in range(n):
        ts = start + timedelta(minutes=15 * (i + 1))
        engine.step(_bar(ts, o=px, h=px + 2, low=px, c=px))


def _open_ny_pair(engine: ClosedBarEngine) -> None:
    engine.step(_bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000))
    engine.step(_bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2010))
    engine.step(_bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010))


def test_bar_range_incumbent_is_unchanged_without_atr_history() -> None:
    engine = _engine(stop_mode="bar_range")
    _open_ny_pair(engine)
    assert engine.pairs[0].sl_dist == pytest.approx(20.0)


def test_atr14_stop_is_sl_mult_times_wilder_atr() -> None:
    engine = _engine(stop_mode="atr14", sl_mult=2.0)
    _warmup_constant_tr(engine, ATR14_PERIOD + 1)
    _open_ny_pair(engine)
    atr_at_fill = wilder_atr(engine._bars[:-1])
    assert atr_at_fill is not None
    assert engine.pairs[0].sl_dist == pytest.approx(atr_at_fill * 2.0)


def test_orb_atr14_blend_is_fifty_fifty_then_sl_mult() -> None:
    engine = _engine(stop_mode="orb_atr14_blend", sl_mult=2.0)
    _warmup_constant_tr(engine, ATR14_PERIOD + 1)
    _open_ny_pair(engine)
    atr_at_fill = wilder_atr(engine._bars[:-1])
    assert atr_at_fill is not None
    expected = blended_orb_atr(10.0, atr_at_fill) * 2.0
    assert engine.pairs[0].sl_dist == pytest.approx(expected)


def test_atr_modes_skip_when_history_is_short() -> None:
    engine = _engine(stop_mode="atr14")
    _open_ny_pair(engine)
    assert engine.pairs == []
    assert engine.suppressed_signal_reasons["insufficient_atr"] == 1


def test_atr_history_survives_snapshot_restore_of_legacy_payloads() -> None:
    engine = _engine(stop_mode="atr14")
    _warmup_constant_tr(engine, ATR14_PERIOD + 1)
    payload = engine.snapshot()
    assert len(payload["bars"]) == ATR14_PERIOD + 1
    restored = _engine(stop_mode="atr14")
    restored.restore(payload)
    _open_ny_pair(restored)
    atr_at_fill = wilder_atr(restored._bars[:-1])
    assert atr_at_fill is not None
    assert restored.pairs[0].sl_dist == pytest.approx(atr_at_fill * 2.0)

    legacy = _engine(stop_mode="atr14")
    legacy.restore({k: v for k, v in payload.items() if k != "bars"})
    assert legacy._bars == []
    _open_ny_pair(legacy)
    assert legacy.pairs == []


def test_stop_mode_serializes_through_settings_and_params() -> None:
    assert Settings().engine_params().stop_mode == "bar_range"
    params = Settings(stop_mode="orb_atr14_blend").engine_params()
    assert params.stop_mode == "orb_atr14_blend"
    report = _engine(stop_mode="atr14").report("XAUUSD", Timeframe.M15, "local")
    assert report.stop_mode == "atr14"
    assert report.report_header.stop_mode == "atr14"
