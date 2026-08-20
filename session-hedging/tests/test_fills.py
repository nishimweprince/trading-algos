from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import ClosedBarEngine
from fills import (
    TickPathUnavailable,
    _fill_limit,
    _fill_stop,
    after_lock_same_bar,
    covering_status,
    fill_inside_ohlc,
    m1_covering,
    resolve_bar_levels,
    resolve_oco_trigger,
)
from models import Candle, EngineParams, IntrabarMode, Timeframe
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


def _same_bar() -> Candle:
    # Open 2000, trades down through a locked long stop at 2002? Wait: long lock SL is above entry.
    # After short is stopped, long is locked to entry+2 = 2002. Bar also reaches long TP at 2012.
    return _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2003,
        h=2015,
        low=2000,
        c=2008,
    )


def test_optimistic_prefers_tp_when_lock_stop_and_tp_both_hit() -> None:
    bar = _same_bar()
    hit = after_lock_same_bar(
        mode=IntrabarMode.OPTIMISTIC,
        is_long=True,
        bar=bar,
        stop=2002,
        tp=2012,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.kind == "tp"
    assert hit.fill == 2012


def test_pessimistic_prefers_stop_when_lock_stop_and_tp_both_hit() -> None:
    bar = _same_bar()
    hit = after_lock_same_bar(
        mode=IntrabarMode.PESSIMISTIC,
        is_long=True,
        bar=bar,
        stop=2002,
        tp=2012,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.kind == "stop"
    assert hit.fill == 2002


def test_m1_conservative_without_m1_matches_pessimistic() -> None:
    bar = _same_bar()
    hit = after_lock_same_bar(
        mode=IntrabarMode.M1_CONSERVATIVE,
        is_long=True,
        bar=bar,
        stop=2002,
        tp=2012,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.kind == "stop"


def test_m1_walks_path_and_conservative_takes_stop_first() -> None:
    parent = _same_bar()
    start = parent.ts - timedelta(minutes=15)
    both = _bar(start + timedelta(minutes=1), o=2001, h=2015, low=2000, c=2008)
    cons = after_lock_same_bar(
        mode=IntrabarMode.M1_CONSERVATIVE,
        is_long=True,
        bar=parent,
        stop=2002,
        tp=2012,
        m1_bars=[both],
        parent_minutes=15,
    )
    raw_m1 = after_lock_same_bar(
        mode=IntrabarMode.M1,
        is_long=True,
        bar=parent,
        stop=2002,
        tp=2012,
        m1_bars=[both],
        parent_minutes=15,
    )
    assert cons.kind == "stop"
    assert raw_m1.kind == "tp"


def test_tick_mode_is_interface_only() -> None:
    with pytest.raises(TickPathUnavailable):
        ClosedBarEngine(
            build_windows(["new_york"], {}),
            EngineParams(intrabar_mode=IntrabarMode.TICK, orb_minutes=15, timeframe_minutes=15),
        )


def test_engine_default_is_m1_conservative_and_reports_same_bar_fields() -> None:
    params = EngineParams(orb_minutes=15, entry_delay_minutes=15, timeframe_minutes=15)
    assert params.intrabar_mode is IntrabarMode.M1_CONSERVATIVE
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    engine.prev_in_session["new_york"] = True
    from engine import Pair

    pair = Pair(
        id="new_york:same-bar",
        session="new_york",
        entry=2000,
        sl_dist=4,
        long_sl=1996,
        long_tp=2012,
        short_sl=2004,
        short_tp=1988,
        primary_side="long",
        entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        long_entry=2000,
        short_entry=2000,
    )
    engine.pairs.append(pair)
    engine.step(_bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2000, h=2015, low=1999, c=2005))
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert pair.same_bar_resolved is True
    assert report.same_bar_resolution_rate == pytest.approx(1.0)
    assert isinstance(report.same_bar_r, float)
    ny = next(row for row in report.session_anchor_stats if row.session == "new_york")
    assert ny.same_bar_resolution_rate == pytest.approx(1.0)
    assert ny.same_bar_r == pytest.approx(report.same_bar_r)


@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize("open_", [90.0, 100.0, 110.0])
def test_stop_loss_never_better_than_level_and_adverse_gap_fills_at_open(
    is_long: bool, open_: float
) -> None:
    level = 100.0
    fill = _fill_stop(open_, level, is_long=is_long)
    if is_long:
        assert fill <= level
        if open_ < level:
            assert fill == open_
        else:
            assert fill == level
    else:
        assert fill >= level
        if open_ > level:
            assert fill == open_
        else:
            assert fill == level


@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize("open_", [90.0, 110.0, 115.0])
def test_limit_permits_level_or_better_on_a_favorable_opening_gap(
    is_long: bool, open_: float
) -> None:
    level = 110.0 if is_long else 90.0
    fill = _fill_limit(open_, level, is_long=is_long)
    if is_long:
        assert fill >= level
        if open_ > level:
            assert fill == open_
        else:
            assert fill == level
    else:
        assert fill <= level
        if open_ < level:
            assert fill == open_
        else:
            assert fill == level


@pytest.mark.parametrize("mode", ["optimistic", "pessimistic", "m1", "m1_conservative"])
@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize("open_", [85.0, 100.0, 115.0])
def test_resolver_fills_are_ohlc_consistent_and_honor_the_frozen_contract(
    mode: str, is_long: bool, open_: float
) -> None:
    stop, tp = (100.0, 110.0) if is_long else (100.0, 90.0)
    bar = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=open_,
        h=max(open_, 116.0),
        low=min(open_, 84.0),
        c=101.0,
    )
    hit = resolve_bar_levels(
        mode=mode,
        is_long=is_long,
        bar=bar,
        stop=stop,
        tp=tp,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.fill is not None
    assert fill_inside_ohlc(bar, hit.fill)
    if hit.kind == "stop":
        assert hit.fill <= stop if is_long else hit.fill >= stop
        if (is_long and open_ < stop) or (not is_long and open_ > stop):
            assert hit.fill == open_
    if hit.kind == "tp":
        assert hit.fill >= tp if is_long else hit.fill <= tp
        if (is_long and open_ > tp) or (not is_long and open_ < tp):
            assert hit.fill == open_


@pytest.mark.parametrize("mode", ["optimistic", "pessimistic", "m1", "m1_conservative"])
@pytest.mark.parametrize("bullish", [True, False])
@pytest.mark.parametrize("open_", [90.0, 100.0, 110.0])
def test_stop_entry_never_better_than_trigger_and_adverse_gap_fills_at_open(
    mode: str, bullish: bool, open_: float
) -> None:
    upper, lower = 105.0, 95.0
    bar = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=open_,
        h=max(open_, 112.0),
        low=min(open_, 88.0),
        c=101.0,
    )
    hit = resolve_oco_trigger(
        mode=mode,
        bullish_signal=bullish,
        bar=bar,
        upper=upper,
        lower=lower,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.fill is not None
    assert fill_inside_ohlc(bar, hit.fill)
    if hit.side == "long":
        assert hit.fill >= upper
        if open_ > upper:
            assert hit.fill == open_
    else:
        assert hit.fill <= lower
        if open_ < lower:
            assert hit.fill == open_


def test_covering_status_reports_absent_partial_and_complete() -> None:
    parent = _same_bar()
    start = parent.ts - timedelta(minutes=15)
    absent = m1_covering(parent, [], 15)
    partial = m1_covering(parent, [_bar(start + timedelta(minutes=1), o=1, h=2, low=1, c=1)], 15)
    complete = [
        _bar(start + timedelta(minutes=offset), o=1, h=2, low=1, c=1) for offset in range(1, 16)
    ]
    assert covering_status(absent, 15) == "absent"
    assert covering_status(partial, 15) == "partial"
    assert covering_status(m1_covering(parent, complete, 15), 15) == "complete"
