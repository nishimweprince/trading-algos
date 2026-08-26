"""Lock modes: none, breakeven, r_relative, with incumbent absolute 20-pip preserved."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import Candle, EngineParams, Timeframe
from backtesting_service.sessions import build_windows


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


def _engine(**kwargs: object) -> ClosedBarEngine:
    return ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            pip_size=0.1,
            lock_pips=20,
            lock_mode=str(kwargs.get("lock_mode", "absolute")),
            lock_r=float(kwargs.get("lock_r", 0.0)),  # type: ignore[arg-type]
            orb_minutes=15,
            timeframe_minutes=15,
            entry_delay_minutes=15,
            anchor_tolerance_minutes=15,
            intrabar_mode="optimistic",
        ),
    )


def _stop_one_side_for_large_s(*, lock_mode: str, lock_r: float = 0.0):
    engine = _engine(lock_mode=lock_mode, lock_r=lock_r)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2000.1, low=1999.9, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2002, low=2000, c=2001.5)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.5, low=1999.8, c=2000.2)
    stop_short = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2001,
        h=2005,
        low=2000.5,
        c=2004.5,
    )
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    engine.step(stop_short)
    return engine.pairs[0]


def test_incumbent_absolute_lock_is_entry_plus_twenty_pips() -> None:
    pair = _stop_one_side_for_large_s(lock_mode="absolute")
    assert pair.locked is True
    assert pair.short_open is False
    assert pair.long_open is True
    assert pair.long_sl == pytest.approx(pair.entry + 2.0)


def test_lock_mode_none_leaves_the_original_stop() -> None:
    pair = _stop_one_side_for_large_s(lock_mode="none")
    assert pair.locked is True
    assert pair.long_open is True
    assert pair.long_sl == pytest.approx(pair.entry - pair.sl_dist)


def test_lock_mode_breakeven_moves_to_entry() -> None:
    pair = _stop_one_side_for_large_s(lock_mode="breakeven")
    assert pair.long_sl == pytest.approx(pair.entry)


def test_lock_mode_r_relative_uses_lock_r_times_s() -> None:
    pair = _stop_one_side_for_large_s(lock_mode="r_relative", lock_r=0.1)
    assert pair.long_sl == pytest.approx(pair.entry + 0.1 * pair.sl_dist)
    pair2 = _stop_one_side_for_large_s(lock_mode="r_relative", lock_r=0.2)
    assert pair2.long_sl == pytest.approx(pair2.entry + 0.2 * pair2.sl_dist)


def test_lock_mode_serializes_and_requires_lock_r() -> None:
    assert Settings().engine_params().lock_mode == "absolute"
    assert Settings().engine_params().lock_r == 0.0
    params = Settings(lock_mode="breakeven").engine_params()
    assert params.lock_mode == "breakeven"
    with pytest.raises(ValidationError, match="LOCK_R"):
        EngineParams(lock_mode="r_relative")
    dumped = EngineParams(lock_mode="r_relative", lock_r=0.2)
    assert EngineParams.model_validate(dumped.model_dump()).lock_r == pytest.approx(0.2)
    report = _engine(lock_mode="none").report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.lock_mode == "none"
    assert report.report_header.lock_r == pytest.approx(0.0)
