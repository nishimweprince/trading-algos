from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import ClosedBarEngine, Pair
from firm_profile import FirmProfile
from models import Candle, EngineParams, Timeframe
from risk_guards import PropGuard
from sessions import build_windows


def _bar(ts: datetime, *, o: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=high,
        low=low,
        close=close,
        volume=1,
        provider="test",
        source_instrument="XAUUSD",
    )


def _guarded_engine() -> ClosedBarEngine:
    return ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            orb_minutes=15,
            timeframe_minutes=15,
            dollars_per_pip_per_qty=10,
            firm_profile="custom",
            firm_initial_balance=100_000,
            firm_daily_loss_limit_pct=1.0,
            firm_total_loss_limit_pct=10.0,
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ),
    )


def test_floating_drawdown_trips_guard_without_closed_loss() -> None:
    engine = _guarded_engine()
    entry_ts = datetime(2026, 1, 14, 14, 30, tzinfo=UTC)
    pair = Pair(
        id="new_york:floating",
        session="new_york",
        entry=100,
        sl_dist=20,
        long_sl=80,
        long_tp=160,
        short_sl=120,
        short_tp=40,
        short_open=False,
        entry_ts=entry_ts,
    )
    engine.pairs.append(pair)
    engine.step(
        _bar(
            entry_ts + timedelta(minutes=15),
            o=100,
            high=100,
            low=89,
            close=89,
        )
    )
    assert engine.trades == []
    assert pair.long_open is True
    assert engine.prop_guard.state.breached is True
    assert engine.prop_guard.state.breach_reason == "daily_loss_limit"
    assert engine.prop_guard.state.last_equity_cash == pytest.approx(98_900)
    assert any(event.kind == "prop_guard_breached" for event in engine.events)

    assert (
        engine._open_pair(
            "new_york", 89, 1, entry_ts + timedelta(minutes=30), bullish=True
        )
        is False
    )
    assert engine.suppressed_signal_reasons["prop_guard"] == 1


def test_guard_does_not_rewrite_closed_history_and_persists() -> None:
    engine = _guarded_engine()
    ts = datetime(2026, 1, 14, 14, 30, tzinfo=UTC)
    closed = Pair(
        id="new_york:closed",
        session="new_york",
        entry=100,
        sl_dist=10,
        long_sl=90,
        long_tp=130,
        short_sl=110,
        short_tp=70,
        short_open=False,
        entry_ts=ts,
    )
    engine.pairs.append(closed)
    engine._close_long(closed, 101, ts + timedelta(minutes=15))
    history_before = [trade.model_dump() for trade in engine.trades]

    floating = Pair(
        id="new_york:floating-after-close",
        session="new_york",
        entry=100,
        sl_dist=30,
        long_sl=70,
        long_tp=190,
        short_sl=130,
        short_tp=10,
        short_open=False,
        entry_ts=ts,
    )
    engine.pairs.append(floating)
    engine._mark_equity(88, ts + timedelta(minutes=30))
    assert engine.prop_guard.state.breached is True
    assert [trade.model_dump() for trade in engine.trades] == history_before

    restored = _guarded_engine()
    restored.restore(engine.snapshot())
    assert restored.prop_guard.state.breached is True
    assert restored.prop_guard.state.breach_reason == engine.prop_guard.state.breach_reason
    assert restored.prop_guard.state.breached_at == engine.prop_guard.state.breached_at
    report = restored.report("XAUUSD", Timeframe.M15, "local")
    assert report.prop_guard_breached is True
    assert report.prop_guard_breach_reason == "daily_loss_limit"


def test_daily_reference_resets_at_first_mark_after_boundary() -> None:
    guard = PropGuard(
        FirmProfile(
            initial_balance=100_000,
            daily_loss_limit_pct=5,
            total_loss_limit_pct=20,
            timezone="America/New_York",
            daily_reset_time="00:00",
        )
    )
    assert guard.evaluate(datetime(2026, 1, 14, 17, tzinfo=UTC), 100_000) is False
    assert guard.state.daily_reference_equity == pytest.approx(100_000)
    # 00:01 EST on the next day: the first mark becomes the new reference.
    assert guard.evaluate(datetime(2026, 1, 15, 5, 1, tzinfo=UTC), 99_000) is False
    assert guard.state.daily_reference_equity == pytest.approx(99_000)
    assert guard.evaluate(datetime(2026, 1, 15, 18, tzinfo=UTC), 94_049) is True
    assert guard.state.breach_reason == "daily_loss_limit"
