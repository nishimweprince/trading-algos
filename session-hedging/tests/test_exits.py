from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import ClosedBarEngine, Pair
from exits import time_exit_due
from models import Candle, EngineParams, TimeExitMode, Timeframe
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


def _engine(*, mode: str = "pessimistic", time_exit_mode: str = "max_age") -> ClosedBarEngine:
    return ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            orb_minutes=15,
            timeframe_minutes=15,
            intrabar_mode=mode,
            time_exit_mode=time_exit_mode,
            max_age_hours=1,
            one_open_per_session=False,
            max_concurrent_structures=0,
        ),
    )


def _single_long(entry_ts: datetime) -> Pair:
    return Pair(
        id=f"new_york:{entry_ts.isoformat()}",
        session="new_york",
        entry=100,
        sl_dist=10,
        long_sl=90,
        long_tp=130,
        short_sl=110,
        short_tp=70,
        short_open=False,
        entry_ts=entry_ts,
    )


def test_time_exit_is_strictly_first_bar_close_past_age_from_entry() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine()
    pair = _single_long(entry_ts)
    engine.pairs.append(pair)
    exact = _bar(entry_ts + timedelta(hours=1), o=100, high=101, low=99, close=100)
    engine.step(exact)
    assert pair.long_open is True
    first_past = _bar(
        entry_ts + timedelta(hours=1, minutes=15),
        o=100,
        high=102,
        low=99,
        close=101,
    )
    engine.step(first_past)
    assert pair.long_open is False
    assert engine.trades[-1].reason == "time_exit"
    assert engine.trades[-1].exit == pytest.approx(101)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.outcome_mix.time_exit == pytest.approx(1.0)
    assert report.outcome_mix.whipsaw == 0.0
    assert report.outcome_mix.lock == 0.0


def test_time_exit_mode_none_leaves_aged_pair_open() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(time_exit_mode="none")
    pair = _single_long(entry_ts)
    engine.pairs.append(pair)
    engine.step(
        _bar(
            entry_ts + timedelta(hours=2),
            o=100,
            high=101,
            low=99,
            close=100,
        )
    )
    assert pair.long_open is True


@pytest.mark.parametrize(
    ("mode", "expected_exit"),
    [("optimistic", 130.0), ("pessimistic", 90.0)],
)
def test_resolver_ladder_wins_when_time_exit_stop_and_target_collide(
    mode: str, expected_exit: float
) -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(mode=mode)
    pair = _single_long(entry_ts)
    engine.pairs.append(pair)
    engine.step(
        _bar(
            entry_ts + timedelta(hours=1, minutes=15),
            o=100,
            high=131,
            low=89,
            close=105,
        )
    )
    assert engine.trades[-1].exit == pytest.approx(expected_exit)
    assert engine.trades[-1].reason == "sl_or_tp"


def test_time_exit_predicate_uses_strict_past_boundary() -> None:
    entry = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    assert not time_exit_due(
        entry_ts=entry,
        bar_close_ts=entry + timedelta(hours=24),
        mode=TimeExitMode.MAX_AGE,
        max_age_hours=24,
    )
    assert time_exit_due(
        entry_ts=entry,
        bar_close_ts=entry + timedelta(hours=24, minutes=1),
        mode=TimeExitMode.MAX_AGE,
        max_age_hours=24,
    )
