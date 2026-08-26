"""Single-topic entry filters. Defaults off; each skip is attributed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.filters import (
    D1_EMA_PERIOD,
    DailyCloseTracker,
    d1_direction_allows,
    ema,
    entry_filter_reason,
    is_nr7,
    orb_atr_ratio,
)
from backtesting_service.indicators import ATR14_PERIOD, wilder_atr
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
            orb_minutes=15,
            timeframe_minutes=15,
            entry_delay_minutes=15,
            anchor_tolerance_minutes=15,
            filter_d1_ema50=bool(kwargs.get("filter_d1_ema50", False)),
            filter_nr7=bool(kwargs.get("filter_nr7", False)),
            filter_orb_atr_min=float(kwargs.get("filter_orb_atr_min", 0.0)),  # type: ignore[arg-type]
            filter_orb_atr_max=float(kwargs.get("filter_orb_atr_max", 0.0)),  # type: ignore[arg-type]
        ),
    )


def _open_ny_pair(
    engine: ClosedBarEngine, *, signal_high: float = 2010.0, close: float = 2008.0
) -> None:
    engine.step(_bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000))
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=signal_high, low=2000, c=close)
    )
    engine.step(_bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010))


def _warmup_constant_tr(engine: ClosedBarEngine, n: int) -> None:
    start = datetime(2026, 1, 14, 8, 0, tzinfo=UTC)
    px = 1990.0
    for i in range(n):
        ts = start + timedelta(minutes=15 * (i + 1))
        engine.step(_bar(ts, o=px, h=px + 2, low=px, c=px))


def test_ema_seeds_with_sma_then_smooths() -> None:
    values = [float(i) for i in range(1, 53)]
    seeded = sum(values[:50]) / 50
    k = 2.0 / 51
    expected = values[50] * k + seeded * (1 - k)
    expected = values[51] * k + expected * (1 - k)
    assert ema(values, 50) == pytest.approx(expected)
    assert ema(values[:49], 50) is None


def test_d1_direction_and_nr7_predicates() -> None:
    assert d1_direction_allows(bullish=True, prior_close=101, ema50=100)
    assert not d1_direction_allows(bullish=True, prior_close=99, ema50=100)
    assert d1_direction_allows(bullish=False, prior_close=99, ema50=100)
    assert not d1_direction_allows(bullish=True, prior_close=100, ema50=100)
    assert is_nr7([5, 4, 3, 3, 2, 4, 1]) is True
    assert is_nr7([1, 1, 1, 1, 1, 1, 2]) is False
    assert is_nr7([1, 2, 3, 4, 5, 6]) is None
    assert orb_atr_ratio(10, 2) == pytest.approx(5.0)


def test_entry_filter_reason_is_noop_when_disabled() -> None:
    assert (
        entry_filter_reason(
            filter_d1_ema50=False,
            filter_nr7=False,
            filter_orb_atr_min=0,
            filter_orb_atr_max=0,
            entry_hours_utc_exclude=frozenset(),
            ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
            bullish=True,
            range_price=10,
            session_orb_ranges=[],
            prior_d1=None,
            atr=None,
        )
        is None
    )


def test_disabled_filters_leave_hedge_pair_unchanged() -> None:
    engine = _engine()
    _open_ny_pair(engine)
    assert len(engine.pairs) == 1
    assert engine.trades_skipped_by_filter == 0
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.trades_skipped_by_filter == 0
    assert report.report_header.filter_d1_ema50 is False
    assert report.report_header.filter_nr7 is False


def test_d1_ema50_skips_until_history_exists_then_blocks_countertrend() -> None:
    engine = _engine(filter_d1_ema50=True)
    _open_ny_pair(engine)
    assert engine.pairs == []
    assert engine.trades_skipped_by_filter == 1
    assert engine.suppressed_signal_reasons["insufficient_d1"] == 1
    engine._d1.closes = [150.0 - i for i in range(D1_EMA_PERIOD)]
    later = _engine(filter_d1_ema50=True)
    later._d1.closes = list(engine._d1.closes)
    _open_ny_pair(later)
    assert later.pairs == []
    assert later.suppressed_signal_reasons["filter_d1_ema50"] == 1
    aligned = _engine(filter_d1_ema50=True)
    aligned._d1.closes = [100.0 + i for i in range(D1_EMA_PERIOD)]
    _open_ny_pair(aligned)
    assert len(aligned.pairs) == 1


def test_daily_close_tracker_finalizes_the_prior_utc_day_on_the_next_bar() -> None:
    tracker = DailyCloseTracker()
    day1 = datetime(2026, 1, 14, 23, 45, tzinfo=UTC)
    day2 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    tracker.observe(_bar(day1, o=10, h=11, low=9, c=10.5))
    assert tracker.closes == []
    tracker.observe(_bar(day2, o=10.5, h=11, low=10, c=10.8))
    assert tracker.closes[0] == pytest.approx(10.5)


def test_nr7_needs_seven_same_session_orbs_and_requires_the_narrowest() -> None:
    engine = _engine(filter_nr7=True)
    day = datetime(2026, 1, 14, tzinfo=UTC)
    session_days = 0
    while session_days < 8:
        if day.weekday() < 5:
            session_days += 1
            narrow = session_days == 8
            high = 2001.0 if narrow else 2010.0 + session_days
            close = 2000.8 if narrow else 2008.0
            engine.step(_bar(day.replace(hour=13, minute=0), o=2000, h=2001, low=1999, c=2000))
            engine.step(_bar(day.replace(hour=13, minute=15), o=2000, h=high, low=2000, c=close))
            engine.step(
                _bar(day.replace(hour=13, minute=30), o=2000.5, h=2001, low=2000.2, c=2000.6)
            )
        day += timedelta(days=1)
    assert engine.suppressed_signal_reasons["insufficient_nr7"] == 6
    assert engine.suppressed_signal_reasons["filter_nr7"] == 1
    assert len(engine.pairs) == 1
    assert any(event.kind == "signal_skipped_filter" for event in engine.events)


def test_orb_atr_min_and_max_skip_when_the_ratio_is_outside_the_bound() -> None:
    too_narrow = _engine(filter_orb_atr_min=0.5)
    _warmup_constant_tr(too_narrow, ATR14_PERIOD + 1)
    _open_ny_pair(too_narrow, signal_high=2000.4, close=2000.3)
    atr = wilder_atr(too_narrow._bars[:-1])
    assert atr is not None
    assert 0.4 / atr < 0.5
    assert too_narrow.pairs == []
    assert too_narrow.suppressed_signal_reasons["filter_orb_atr_min"] == 1

    too_wide = _engine(filter_orb_atr_max=2.0)
    _warmup_constant_tr(too_wide, ATR14_PERIOD + 1)
    _open_ny_pair(too_wide)
    atr_wide = wilder_atr(too_wide._bars[:-1])
    assert atr_wide is not None
    assert 10.0 / atr_wide > 2.0
    assert too_wide.pairs == []
    assert too_wide.suppressed_signal_reasons["filter_orb_atr_max"] == 1

    short = _engine(filter_orb_atr_min=0.5)
    _open_ny_pair(short)
    assert short.suppressed_signal_reasons["insufficient_atr"] == 1


def test_d1_state_survives_snapshot_and_defaults_stay_off() -> None:
    engine = _engine(filter_d1_ema50=True)
    engine._d1.closes = [100.0 + i for i in range(D1_EMA_PERIOD)]
    payload = engine.snapshot()
    restored = _engine(filter_d1_ema50=True)
    restored.restore(payload)
    _open_ny_pair(restored)
    assert len(restored.pairs) == 1
    params = Settings().engine_params()
    assert params.filter_d1_ema50 is False
    assert params.filter_nr7 is False
    assert params.filter_orb_atr_min == pytest.approx(0.0)
    assert params.filter_orb_atr_max == pytest.approx(0.0)
    enabled = Settings(filter_nr7=True, filter_orb_atr_min=0.5).engine_params()
    assert enabled.filter_nr7 is True
    assert enabled.filter_orb_atr_min == pytest.approx(0.5)
