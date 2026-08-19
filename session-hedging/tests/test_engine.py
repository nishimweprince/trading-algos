from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import ClosedBarEngine, bar_open
from models import Candle, EngineParams, Timeframe
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


def _engine(sessions: list[str] | None = None, **kwargs: float | bool | int) -> ClosedBarEngine:
    params = EngineParams(
        pip_size=float(kwargs.get("pip_size", 0.1)),
        sl_mult=float(kwargs.get("sl_mult", 2.0)),
        rr=float(kwargs.get("rr", 3.0)),
        min_stop_pips=float(kwargs.get("min_stop_pips", 0.0)),
        lock_pips=float(kwargs.get("lock_pips", 20.0)),
        qty=float(kwargs.get("qty", 1.0)),
        skip_doji=bool(kwargs.get("skip_doji", True)),
        timeframe_minutes=int(kwargs.get("timeframe_minutes", 15)),
    )
    names = sessions or ["new_york"]
    return ClosedBarEngine(build_windows(names, {}), params)


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
