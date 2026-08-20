from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import ClosedBarEngine, Pair, bar_open
from models import Candle, EngineParams, StrategyMode, Timeframe
from sessions import build_windows


def _bar(
    ts: datetime,
    *,
    o: float,
    h: float,
    low: float,
    c: float,
    symbol: str = "XAUUSD",
) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        provider="test",
        source_instrument=symbol,
    )


def _engine(sessions: list[str] | None = None, **kwargs: object) -> ClosedBarEngine:
    dollars_per_pip = kwargs.get("dollars_per_pip_per_qty")
    params = EngineParams(
        pip_size=float(kwargs.get("pip_size", 0.1)),  # type: ignore[arg-type]
        sl_mult=float(kwargs.get("sl_mult", 2.0)),  # type: ignore[arg-type]
        rr=float(kwargs.get("rr", 3.0)),  # type: ignore[arg-type]
        min_stop_pips=float(kwargs.get("min_stop_pips", 0.0)),  # type: ignore[arg-type]
        lock_pips=float(kwargs.get("lock_pips", 20.0)),  # type: ignore[arg-type]
        qty=float(kwargs.get("qty", 1.0)),  # type: ignore[arg-type]
        skip_doji=bool(kwargs.get("skip_doji", True)),
        timeframe_minutes=int(kwargs.get("timeframe_minutes", 15)),  # type: ignore[arg-type]
        dollars_per_pip_per_qty=(
            float(dollars_per_pip) if dollars_per_pip is not None else None
        ),
        strategy_mode=StrategyMode(str(kwargs.get("strategy_mode", "lock_survivor"))),
        signal_delay_bars=int(kwargs.get("signal_delay_bars", 0) or 0),
        trail_step_pips=float(kwargs.get("trail_step_pips", 0.0) or 0.0),
        max_stop_pips=float(kwargs.get("max_stop_pips", 0.0) or 0.0),
        max_open_pairs=int(kwargs.get("max_open_pairs", 0) or 0),
        flatten_at_session_end=bool(kwargs.get("flatten_at_session_end", False)),
    )
    names = sessions or ["new_york"]
    return ClosedBarEngine(build_windows(names, {}), params)


def _sweep(**kwargs: object) -> ClosedBarEngine:
    defaults: dict[str, object] = {
        "strategy_mode": "sweep_fade",
        "signal_delay_bars": 2,
        "trail_step_pips": 50,
        "lock_pips": 20,
    }
    defaults.update(kwargs)
    return _engine(["new_york"], **defaults)


def test_bar_open_is_interval_start() -> None:
    ts = datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    bar = _bar(ts, o=1, h=2, low=0.5, c=1.5)
    assert bar_open(bar, 15) == datetime(2026, 1, 14, 13, 0, tzinfo=UTC)


def test_ny_first_bar_uses_open_not_previous_close() -> None:
    """The 07:45–08:00 NY bar ends at 08:00 and must not be the signal bar."""
    engine = _engine(["new_york"])
    pre = _bar(
        datetime(2026, 1, 14, 13, 0, tzinfo=UTC),  # open 12:45 UTC = 07:45 EST
        o=2000,
        h=2001,
        low=1999,
        c=2000.5,
    )
    first = _bar(
        datetime(2026, 1, 14, 13, 15, tzinfo=UTC),  # open 13:00 UTC = 08:00 EST
        o=2000.5,
        h=2010,
        low=2000,
        c=2008,
    )
    engine.step(pre)
    assert engine.pending == {}
    engine.step(first)
    assert "new_york" in engine.pending
    assert engine.pending["new_york"].range_price == 10.0


def test_fill_at_next_bar_open() -> None:
    engine = _engine(["new_york"])
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010)
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    assert len(engine.pairs) == 1
    assert engine.pairs[0].entry == 2009.0
    assert engine.pairs[0].sl_dist == 20.0  # 2 * range 10


def test_doji_signal_bar_is_skipped() -> None:
    engine = _engine(["new_york"])
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    doji = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=1990, c=2000)
    engine.step(pre)
    engine.step(doji)
    assert engine.pending == {}


def test_both_stops_same_bar_no_lock() -> None:
    engine = _engine(["new_york"], lock_pips=20)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2002, low=2000, c=2001)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2010, low=1990, c=2000)
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    pair = engine.pairs[0]
    assert pair.long_open is False
    assert pair.short_open is False
    assert pair.locked is False
    assert engine.stats.locks == 0
    assert engine.stats.long_loss == 1
    assert engine.stats.short_loss == 1


def test_lock_to_breakeven_when_s_below_lock_pips() -> None:
    engine = _engine(["new_york"], lock_pips=20, pip_size=0.1)
    # range 0.5 → S=1.0; lock dist = 2.0, so BE lock.
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2000.1, low=1999.9, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2000.5, low=2000, c=2000.4)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.2, low=2000, c=2000.1)
    stop_long = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2000,
        h=2000.1,
        low=1998.9,  # through long SL at 2000 - 1.0 = 1999
        c=1999,
    )
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    engine.step(stop_long)
    pair = engine.pairs[0]
    assert pair.locked is True
    assert pair.long_open is False
    assert pair.short_open is True
    assert pair.short_sl == pair.entry  # BE


def test_lock_plus_twenty_pips_when_s_large_enough() -> None:
    engine = _engine(["new_york"], lock_pips=20, pip_size=0.1)
    # range 2.0 → S=4.0 >= lock 2.0
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2000.1, low=1999.9, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2002, low=2000, c=2001.5)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.5, low=1999.8, c=2000.2)
    stop_short = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2001,
        h=2005,  # through short SL at 2000+4=2004
        low=2000.5,
        c=2004.5,
    )
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    engine.step(stop_short)
    pair = engine.pairs[0]
    assert pair.locked is True
    assert pair.short_open is False
    assert pair.long_open is True
    assert pair.long_sl == pytest.approx(pair.entry + 2.0)


def test_gap_through_stop_fills_at_open() -> None:
    engine = _engine(["new_york"])
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2002, low=2000, c=2001)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.5, low=1999.8, c=2000)
    # S = 4. long SL = 1996. Open gaps through it.
    gap = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=1990, h=1991, low=1989, c=1989.5)
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    engine.step(gap)
    long_exits = [t for t in engine.trades if t.side == "long"]
    assert long_exits[0].exit == 1990.0


def test_be_bucket_from_fill_not_bar_close() -> None:
    engine = _engine(["new_york"], lock_pips=20, pip_size=0.1)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2000.1, low=1999.9, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2000.5, low=2000, c=2000.4)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2000, h=2000.2, low=2000, c=2000.1)
    stop_long = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2000,
        h=2000.1,
        low=1998.9,
        c=1999,
    )
    be_stop = _bar(
        datetime(2026, 1, 14, 14, 0, tzinfo=UTC),
        o=1999.95,
        h=2000.1,
        low=1999.8,
        c=1999.5,
    )
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    engine.step(stop_long)
    engine.step(be_stop)
    short = [t for t in engine.trades if t.side == "short"][0]
    assert short.bucket == "be"
    assert engine.stats.short_be == 1
    assert engine.stats.short_loss == 0


def test_independent_sessions_each_open_a_pair() -> None:
    engine = _engine(["tokyo", "london", "new_york"])
    start = datetime(2026, 1, 13, 23, 45, tzinfo=UTC)
    price = 2650.0
    for i in range(60):
        open_px = price
        ts = start + timedelta(minutes=15 * (i + 1))
        bar = _bar(ts, o=open_px, h=open_px + 0.8, low=open_px - 0.4, c=open_px + 0.3)
        engine.step(bar)
        price = bar.close
    sessions = {p.session for p in engine.pairs}
    assert sessions == {"tokyo", "london", "new_york"}
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.open_pairs + len(report.trades) >= 3


def test_pip_and_dollar_results_use_explicit_conversion() -> None:
    engine = _engine(
        ["new_york"], pip_size=0.1, qty=3, dollars_per_pip_per_qty=2
    )
    pair = Pair(
        id="new_york:test",
        session="new_york",
        entry=2000,
        sl_dist=10,
        long_sl=1990,
        long_tp=2030,
        short_sl=2010,
        short_tp=1970,
        primary_side="long",
        entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
    )
    engine.pairs.append(pair)
    engine._close_long(pair, 1999, datetime(2026, 1, 14, 13, 45, tzinfo=UTC))
    trade = engine.trades[0]
    assert trade.pnl_pips == pytest.approx(-10)
    assert trade.pnl_dollars == pytest.approx(-60)
    assert trade.pnl == pytest.approx(-3)  # Legacy price-delta × quantity value.
    assert _engine(["new_york"], pip_size=0.1, qty=1)._pnl_pips(True, 2000, 1999) == (
        trade.pnl_pips
    )


def test_closed_bar_drawdown_marks_open_leg_at_each_close() -> None:
    engine = _engine(["new_york"], pip_size=0.1)
    engine.prev_in_session["new_york"] = True
    engine.pairs.append(
        Pair(
            id="new_york:drawdown",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=110,
            short_sl=110,
            short_tp=90,
            primary_side="long",
            short_open=False,
            entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        )
    )
    engine.stats.realized_pips = -10
    engine.step(
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=100, h=100, low=99, c=99)
    )
    assert engine.max_drawdown_pips == pytest.approx(20)
    engine.step(
        _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=99, h=103, low=99, c=103)
    )
    engine.step(
        _bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC), o=103, h=103, low=101, c=101)
    )
    assert engine.max_drawdown_pips == pytest.approx(20)


def test_bearish_signal_groups_primary_short_and_hedge_long() -> None:
    engine = _engine(["new_york"], pip_size=0.1)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(
        datetime(2026, 1, 14, 13, 15, tzinfo=UTC),
        o=2000,
        h=2000,
        low=1998,
        c=1999,
    )
    fill = _bar(
        datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        o=2000,
        h=2000.5,
        low=1999.5,
        c=2000,
    )
    stop_long = _bar(
        datetime(2026, 1, 14, 13, 45, tzinfo=UTC),
        o=2000,
        h=2000,
        low=1995,
        c=1997,
    )
    for bar in [pre, signal, fill, stop_long]:
        engine.step(bar)
    pair = engine.report("XAUUSD", Timeframe.M15, "local").trade_pairs[0]
    assert pair.status == "partial"
    assert pair.primary is not None and pair.primary.side == "short"
    assert pair.primary.status == "open"
    assert pair.hedge is not None and pair.hedge.side == "long"
    assert pair.hedge.status == "closed"
    assert pair.hedge.role == "hedge"
    assert engine.trades[0].pair_id == pair.id


def test_restore_old_snapshot_leaves_pair_role_unknown() -> None:
    engine = _engine(["new_york"])
    engine.pairs.append(
        Pair(
            id="new_york:old",
            session="new_york",
            entry=2000,
            sl_dist=4,
            long_sl=1996,
            long_tp=2012,
            short_sl=2004,
            short_tp=1988,
            primary_side="short",
            entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        )
    )
    snapshot = engine.snapshot()
    del snapshot["pairs"][0]["primary_side"]  # type: ignore[index]
    del snapshot["stats"]["realized_pips"]  # type: ignore[index]
    restored = _engine(["new_york"])
    restored.restore(snapshot)
    assert restored.pairs[0].primary_side is None


def test_sweep_fade_fills_on_the_third_bar() -> None:
    engine = _sweep()
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=1998, c=2008)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2008, h=2020, low=2007, c=2018)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2015, h=2016, low=2014, c=2015)
    engine.step(pre)
    engine.step(first)
    assert "new_york" in engine.pending
    assert engine.pairs == []
    engine.step(second)
    assert engine.pairs == []
    assert engine.pending["new_york"].bars_remaining == 1
    engine.step(third)
    assert len(engine.pairs) == 1
    pair = engine.pairs[0]
    assert pair.entry == 2015.0
    assert pair.sweep_high == 2020.0
    assert pair.sweep_low == 1998.0


def test_sweep_fade_fills_on_the_third_h1_bar() -> None:
    engine = _sweep(timeframe_minutes=60)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=2000, h=2010, low=1998, c=2008)
    second = _bar(datetime(2026, 1, 14, 15, 0, tzinfo=UTC), o=2008, h=2020, low=2007, c=2018)
    third = _bar(datetime(2026, 1, 14, 16, 0, tzinfo=UTC), o=2015, h=2016, low=2014, c=2015)
    engine.step(pre)
    engine.step(first)
    assert "new_york" in engine.pending
    assert engine.pairs == []
    engine.step(second)
    assert engine.pairs == []
    engine.step(third)
    pair = engine.pairs[0]
    assert pair.entry == 2015.0
    assert pair.primary_side == "short"
    assert pair.short_open is True
    assert pair.long_open is False


def test_sweep_fade_shorts_after_two_up_bars() -> None:
    engine = _sweep()
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=1998, c=2008)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2008, h=2020, low=2007, c=2018)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2015, h=2016, low=2014, c=2015)
    for bar in (pre, first, second, third):
        engine.step(bar)
    pair = engine.pairs[0]
    assert pair.primary_side == "short"
    assert pair.short_open is True
    assert pair.long_open is False
    assert pair.short_sl == pytest.approx(2020.0)
    assert pair.hedge_pending_side == "long"
    assert pair.hedge_pending_stop == pytest.approx(2020.0)


def test_sweep_fade_invalidation_closes_fade_and_fills_hedge() -> None:
    engine = _sweep()
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=1998, c=2008)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2008, h=2020, low=2007, c=2018)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2015, h=2016, low=2014, c=2015)
    through = _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=2016, h=2025, low=2014, c=2022)
    for bar in (pre, first, second, third, through):
        engine.step(bar)
    pair = engine.pairs[0]
    assert pair.short_open is False
    assert pair.long_open is True
    assert pair.hedge_pending_side is None
    assert pair.long_entry == pytest.approx(2020.0)
    assert engine.trades[0].side == "short"
    assert engine.trades[0].exit == pytest.approx(2020.0)
    assert not (pair.long_open and pair.short_open)


def test_sweep_fade_trails_fifty_pips_after_lock() -> None:
    engine = _sweep()
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2001, low=1990, c=1992)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=1992, h=1993, low=1980, c=1985)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=1988, h=1989, low=1987, c=1988)
    run = _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=1989, h=1995.5, low=1988, c=1994)
    for bar in (pre, first, second, third, run):
        engine.step(bar)
    pair = engine.pairs[0]
    assert pair.primary_side == "long"
    assert pair.locked is True
    assert engine.stats.locks == 1
    # lock at entry+2.0 = 1990, then one 5.0 trail step → 1995
    assert pair.long_sl == pytest.approx(1995.0)


def test_sweep_fade_flattens_at_session_end() -> None:
    engine = _sweep(flatten_at_session_end=True)
    engine.prev_in_session["new_york"] = True
    engine.pairs.append(
        Pair(
            id="new_york:flat",
            session="new_york",
            entry=2000,
            sl_dist=10,
            long_sl=1990,
            long_tp=2030,
            short_sl=2010,
            short_tp=1970,
            primary_side="long",
            short_open=False,
            entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
            long_entry=2000,
        )
    )
    # 22:00 UTC = 17:00 EST, outside NY cash hours.
    engine.step(
        _bar(datetime(2026, 1, 14, 22, 15, tzinfo=UTC), o=2004, h=2005, low=2003, c=2004)
    )
    pair = engine.pairs[0]
    assert pair.long_open is False
    assert engine.trades[0].reason == "session_end"
    assert engine.trades[0].exit == pytest.approx(2004.0)


def test_sweep_fade_skips_when_range_exceeds_max_stop() -> None:
    engine = _sweep(max_stop_pips=80)
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2015, low=1990, c=2012)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2012, h=2025, low=2010, c=2020)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2018, h=2019, low=2017, c=2018)
    for bar in (pre, first, second, third):
        engine.step(bar)
    assert engine.pairs == []


def test_sweep_fade_max_open_pairs_blocks_a_second_fill() -> None:
    engine = _sweep(max_open_pairs=1, trail_step_pips=0, lock_pips=0)
    engine.pairs.append(
        Pair(
            id="new_york:open",
            session="new_york",
            entry=2000,
            sl_dist=4,
            long_sl=1900,
            long_tp=3000,
            short_sl=2100,
            short_tp=1800,
            primary_side="long",
            short_open=False,
            entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
            long_entry=2000,
        )
    )
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    first = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=1998, c=2008)
    second = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2008, h=2020, low=2007, c=2018)
    third = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2015, h=2016, low=2014, c=2015)
    for bar in (pre, first, second, third):
        engine.step(bar)
    assert len(engine.pairs) == 1
    assert engine.pairs[0].id == "new_york:open"
