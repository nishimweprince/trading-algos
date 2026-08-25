from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from engine import ClosedBarEngine, Pair
from models import Candle, EngineParams, Timeframe
from sessions import build_windows


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1,
        provider="test",
        source_instrument="XAUUSD",
    )


def _engine(**updates: object) -> ClosedBarEngine:
    params = EngineParams.model_validate(
        {
            "pip_size": 1,
            "timeframe_minutes": 15,
            "orb_minutes": 15,
            "entry_delay_minutes": 15,
            "intrabar_mode": "pessimistic",
            "time_exit_mode": "none",
            "hedge_path_mode": "chronological_v2",
            "survivor_exit_mode": "unlocked",
        }
        | updates
    )
    return ClosedBarEngine(build_windows(["new_york"], {}), params)


def _pair(ts: datetime) -> Pair:
    return Pair(
        id="new_york:pair",
        session="new_york",
        entry=100,
        sl_dist=10,
        long_sl=90,
        long_tp=130,
        short_sl=110,
        short_tp=70,
        primary_side="long",
        entry_ts=ts,
        long_entry=100,
        short_entry=100,
        long_entry_ts=ts,
        short_entry_ts=ts,
    )


def test_defaults_preserve_legacy_survivor_and_path_modes() -> None:
    params = EngineParams()
    assert params.survivor_exit_mode == "legacy_lock"
    assert params.hedge_path_mode == "legacy_parent_bar"


def test_mfe_trail_activation_must_start_at_or_after_one_r() -> None:
    with pytest.raises(ValidationError, match="at least 1R"):
        EngineParams(survivor_exit_mode="mfe_trail", survivor_trail_activation_r=0.75)


def test_unlocked_survivor_keeps_original_stop_and_reports_activation() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine()
    pair = _pair(ts)
    engine.pairs.append(pair)

    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=111, low=99, c=110))

    assert pair.short_open is False
    assert pair.long_open is True
    assert pair.long_sl == pytest.approx(90)
    assert pair.survivor_side == "long"
    assert pair.survivor_activated_ts == ts + timedelta(minutes=15)
    assert any(event.kind == "survivor_activated" for event in engine.events)


def test_long_mfe_trail_arms_for_next_bar_and_never_loosens() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(
        survivor_exit_mode="mfe_trail",
        survivor_trail_activation_r=1.5,
        survivor_trail_gap_r=1.0,
    )
    pair = _pair(ts)
    engine.pairs.append(pair)
    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=111, low=99, c=110))

    # The bar reaches 1.6R and trades back through the future 0.6R stop. The new
    # stop is armed only after this parent bar, so it cannot use its own low.
    engine._manage_pairs(_bar(ts + timedelta(minutes=30), o=110, h=116, low=105, c=112))
    assert pair.long_open is True
    assert pair.long_sl == pytest.approx(106)
    assert pair.survivor_ratchet_advances == 1

    engine._manage_pairs(_bar(ts + timedelta(minutes=45), o=112, h=114, low=109, c=111))
    assert pair.long_sl == pytest.approx(106)
    engine._manage_pairs(_bar(ts + timedelta(minutes=60), o=108, h=109, low=105, c=106))
    assert pair.long_open is False
    assert engine.trades[-1].exit == pytest.approx(106)


def test_short_mfe_trail_is_symmetric() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(
        survivor_exit_mode="mfe_trail",
        survivor_trail_activation_r=1.5,
        survivor_trail_gap_r=1.0,
    )
    pair = _pair(ts)
    engine.pairs.append(pair)
    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=101, low=89, c=90))
    engine._manage_pairs(_bar(ts + timedelta(minutes=30), o=90, h=95, low=84, c=88))

    assert pair.long_open is False
    assert pair.short_open is True
    assert pair.short_sl == pytest.approx(94)


def test_complete_m1_path_does_not_replay_prices_before_first_stop() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    parent_ts = ts + timedelta(minutes=15)
    children = []
    for minute in range(1, 16):
        child_ts = ts + timedelta(minutes=minute)
        if minute == 1:
            children.append(_bar(child_ts, o=100, h=101, low=95, c=100))
        elif minute == 2:
            children.append(_bar(child_ts, o=100, h=111, low=100, c=110))
        elif minute == 3:
            children.append(_bar(child_ts, o=110, h=110, low=99, c=100))
        else:
            children.append(_bar(child_ts, o=100, h=101, low=99, c=100))
    engine = _engine(
        intrabar_mode="m1_conservative",
        survivor_exit_mode="legacy_lock",
        lock_mode="breakeven",
    )
    engine.m1_bars = children
    pair = _pair(ts)
    engine.pairs.append(pair)

    engine._manage_pairs(_bar(parent_ts, o=100, h=111, low=95, c=100))

    assert [trade.side for trade in engine.trades] == ["short", "long"]
    assert engine.trades[0].ts == ts + timedelta(minutes=2)
    assert engine.trades[1].ts == ts + timedelta(minutes=3)
    assert engine.trades[1].exit == pytest.approx(100)


def test_both_stops_in_one_unresolved_segment_do_not_create_a_survivor() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine()
    pair = _pair(ts)
    engine.pairs.append(pair)
    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=111, low=89, c=100))

    assert not pair.long_open and not pair.short_open
    assert pair.survivor_side is None
    assert not any(event.kind == "survivor_activated" for event in engine.events)


def test_incomplete_m1_coverage_records_each_parent_fallback() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(intrabar_mode="m1_conservative")
    pair = _pair(ts)
    engine.pairs.append(pair)

    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=105, low=95, c=101))

    fallback = next(event for event in engine.events if event.kind == "resolver_fallback")
    assert fallback.detail["coverage"] == "absent"
    assert fallback.detail["hedge_path_mode"] == "chronological_v2"
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.m1_fallback_count == 1


def test_survivor_state_round_trips_through_snapshot() -> None:
    ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _engine(
        survivor_exit_mode="mfe_trail",
        survivor_trail_activation_r=1.5,
        survivor_trail_gap_r=1.0,
    )
    pair = _pair(ts)
    engine.pairs.append(pair)
    engine._manage_pairs(_bar(ts + timedelta(minutes=15), o=100, h=111, low=99, c=110))
    engine._manage_pairs(_bar(ts + timedelta(minutes=30), o=110, h=116, low=110, c=115))

    restored = _engine(
        survivor_exit_mode="mfe_trail",
        survivor_trail_activation_r=1.5,
        survivor_trail_gap_r=1.0,
    )
    restored.restore(engine.snapshot())
    actual = restored.pairs[0]

    assert actual.survivor_side == "long"
    assert actual.survivor_activated_ts == pair.survivor_activated_ts
    assert actual.survivor_ratchet_armed_ts == pair.survivor_ratchet_armed_ts
    assert actual.survivor_post_mfe_pips == pytest.approx(pair.survivor_post_mfe_pips)
    assert actual.survivor_ratchet_advances == pair.survivor_ratchet_advances
    report = restored.report("XAUUSD", Timeframe.M15, "local")
    assert report.trade_pairs[0].survivor_post_failure_mfe_r == pytest.approx(1.6)
