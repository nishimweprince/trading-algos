from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from engine import ClosedBarEngine, Pair, bar_open
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


def _engine(sessions: list[str] | None = None, **kwargs: object) -> ClosedBarEngine:
    dollars_per_pip = kwargs.get("dollars_per_pip_per_qty")
    params = EngineParams(
        pip_size=float(kwargs.get("pip_size", 0.1)),  # type: ignore[arg-type]
        stop_mode=str(kwargs.get("stop_mode", "bar_range")),
        sl_mult=float(kwargs.get("sl_mult", 2.0)),  # type: ignore[arg-type]
        fixed_stop_pips=float(kwargs.get("fixed_stop_pips", 0.0)),  # type: ignore[arg-type]
        rr=float(kwargs.get("rr", 3.0)),  # type: ignore[arg-type]
        min_stop_pips=float(kwargs.get("min_stop_pips", 0.0)),  # type: ignore[arg-type]
        min_stop_cost_mult=float(kwargs.get("min_stop_cost_mult", 0.0)),  # type: ignore[arg-type]
        lock_pips=float(kwargs.get("lock_pips", 20.0)),  # type: ignore[arg-type]
        lock_mode=str(kwargs.get("lock_mode", "absolute")),
        lock_r=float(kwargs.get("lock_r", 0.0)),  # type: ignore[arg-type]
        qty=float(kwargs.get("qty", 1.0)),  # type: ignore[arg-type]
        qty_ref=float(kwargs.get("qty_ref", kwargs.get("qty", 1.0))),  # type: ignore[arg-type]
        skip_doji=bool(kwargs.get("skip_doji", True)),
        timeframe_minutes=int(kwargs.get("timeframe_minutes", 15)),  # type: ignore[arg-type]
        orb_minutes=int(kwargs.get("orb_minutes", 15)),  # type: ignore[arg-type]
        entry_delay_minutes=int(kwargs.get("entry_delay_minutes", 15)),  # type: ignore[arg-type]
        anchor_tolerance_minutes=int(kwargs.get("anchor_tolerance_minutes", 15)),  # type: ignore[arg-type]
        intrabar_mode=str(kwargs.get("intrabar_mode", "optimistic")),
        dollars_per_pip_per_qty=(float(dollars_per_pip) if dollars_per_pip is not None else None),
        cost_model=str(kwargs.get("cost_model", "per_session")),
        spread_pips_per_side=float(kwargs.get("spread_pips_per_side", 0.0)),  # type: ignore[arg-type]
        slippage_pips_per_side=float(kwargs.get("slippage_pips_per_side", 0.0)),  # type: ignore[arg-type]
        commission_pips_per_side=float(kwargs.get("commission_pips_per_side", 0.0)),  # type: ignore[arg-type]
    )
    names = sessions or ["new_york"]
    return ClosedBarEngine(
        build_windows(names, {}),
        params,
        collect_equity_curve=bool(kwargs.get("collect_equity_curve", False)),
    )


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
    assert engine.pairs[0].entry_ts == datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    assert engine.pairs[0].sl_dist == 20.0  # 2 * range 10


def _ny_pair_sl_dist(*, signal_high: float = 2010.0, **params: object) -> float:
    """Open one New York pair whose opening range is ``signal_high - 2000`` and report S."""
    engine = _engine(["new_york"], **params)
    engine.step(_bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000))
    engine.step(
        _bar(
            datetime(2026, 1, 14, 13, 15, tzinfo=UTC),
            o=2000,
            h=signal_high,
            low=2000,
            c=signal_high,
        )
    )
    engine.step(_bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010))
    return engine.pairs[0].sl_dist


def test_fixed_stop_pips_ignores_the_opening_range() -> None:
    """STOP_MODE=fixed_pips pins S, so R is constant across sessions of different width."""
    narrow = _ny_pair_sl_dist(stop_mode="fixed_pips", fixed_stop_pips=150, signal_high=2010.0)
    wide = _ny_pair_sl_dist(stop_mode="fixed_pips", fixed_stop_pips=150, signal_high=2030.0)
    assert narrow == pytest.approx(15.0)  # 150 pips * pip_size 0.1
    assert wide == pytest.approx(15.0)


def test_bar_range_stop_still_scales_with_the_opening_range() -> None:
    narrow = _ny_pair_sl_dist(signal_high=2010.0)
    wide = _ny_pair_sl_dist(signal_high=2030.0)
    assert narrow == pytest.approx(20.0)  # 2 * range 10
    assert wide == pytest.approx(60.0)  # 2 * range 30


def test_min_stop_pips_floors_a_fixed_stop() -> None:
    sl_dist = _ny_pair_sl_dist(stop_mode="fixed_pips", fixed_stop_pips=50, min_stop_pips=200)
    assert sl_dist == pytest.approx(20.0)  # floor 200 pips beats the 50-pip fixed stop


def test_fixed_stop_mode_requires_a_distance() -> None:
    with pytest.raises(ValidationError, match="FIXED_STOP_PIPS"):
        EngineParams(stop_mode="fixed_pips")


def test_report_states_the_stop_mode_cell() -> None:
    engine = _engine(["new_york"], stop_mode="fixed_pips", fixed_stop_pips=150)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.stop_mode == "fixed_pips"
    assert report.fixed_stop_pips == pytest.approx(150.0)


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
        low=1999.4,
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
    engine = _engine(["new_york"], pip_size=0.1, qty=3, dollars_per_pip_per_qty=2)
    pair = Pair(
        id="new_york:test",
        session="new_york",
        entry=2000,
        sl_dist=10,
        long_sl=1990,
        long_tp=2030,
        short_sl=2010,
        short_tp=1970,
        qty=3,
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


def test_mae_mfe_tracks_each_open_leg_and_converts_to_dollars() -> None:
    engine = _engine(["new_york"], pip_size=0.1, qty=3, dollars_per_pip_per_qty=2)
    engine.prev_in_session["new_york"] = True
    pair = Pair(
        id="new_york:excursions",
        session="new_york",
        entry=100,
        sl_dist=10,
        long_sl=90,
        long_tp=110,
        short_sl=110,
        short_tp=90,
        qty=3,
        primary_side="long",
        entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        long_entry=100,
        short_entry=100,
    )
    engine.pairs.append(pair)
    engine.step(_bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=100, h=102, low=97, c=101))
    result = engine.report("XAUUSD", Timeframe.M15, "local").trade_pairs[0]
    assert result.primary is not None
    assert result.primary.mae_pips == pytest.approx(-30)
    assert result.primary.mfe_pips == pytest.approx(20)
    assert result.primary.mae_dollars == pytest.approx(-180)
    assert result.primary.mfe_dollars == pytest.approx(120)
    assert result.hedge is not None
    assert result.hedge.mae_pips == pytest.approx(-20)
    assert result.hedge.mfe_pips == pytest.approx(30)

    engine._close_long(pair, 101, datetime(2026, 1, 14, 14, 0, tzinfo=UTC))
    engine.step(_bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC), o=101, h=105, low=95, c=100))
    closed_primary = engine.report("XAUUSD", Timeframe.M15, "local").trade_pairs[0].primary
    assert closed_primary is not None
    assert closed_primary.status == "closed"
    assert closed_primary.mae_pips == pytest.approx(-30)
    assert closed_primary.mfe_pips == pytest.approx(20)


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
    engine.step(_bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=100, h=100, low=99, c=99))
    assert engine.max_drawdown_pips == pytest.approx(20)
    engine.step(_bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=99, h=103, low=99, c=103))
    engine.step(_bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC), o=103, h=103, low=101, c=101))
    assert engine.max_drawdown_pips == pytest.approx(20)


def test_equity_curve_is_chronological_and_coalesces_duplicate_marks() -> None:
    engine = _engine(["new_york"], pip_size=0.1, collect_equity_curve=True)
    engine.pairs.append(
        Pair(
            id="new_york:curve",
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
    first = datetime(2026, 1, 14, 13, 45, tzinfo=UTC)
    second = datetime(2026, 1, 14, 14, 0, tzinfo=UTC)
    engine._mark_equity(101, first)
    engine._mark_equity(102, second)
    engine._mark_equity(99, second)

    curve = engine._equity_curve()
    assert [point.ts for point in curve] == [first, second]
    assert curve[0].net_equity == pytest.approx(10)
    assert curve[0].net_drawdown == pytest.approx(0)
    assert curve[1].net_equity == pytest.approx(-10)
    assert curve[1].net_drawdown == pytest.approx(30)
    assert engine.net_max_drawdown_pips == pytest.approx(30)


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
    assert pair.hedge.mae_pips == pytest.approx(-50)
    assert pair.hedge.mfe_pips == pytest.approx(5)
    assert pair.primary.mae_pips == pytest.approx(-5)
    assert pair.primary.mfe_pips == pytest.approx(50)
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
    del snapshot["pairs"][0]["long_mae_pips"]  # type: ignore[index]
    del snapshot["pairs"][0]["long_mfe_pips"]  # type: ignore[index]
    del snapshot["pairs"][0]["short_mae_pips"]  # type: ignore[index]
    del snapshot["pairs"][0]["short_mfe_pips"]  # type: ignore[index]
    del snapshot["stats"]["realized_pips"]  # type: ignore[index]
    restored = _engine(["new_york"])
    restored.restore(snapshot)
    assert restored.pairs[0].primary_side is None
    assert restored.pairs[0].long_mae_pips == 0
    assert restored.pairs[0].short_mfe_pips == 0


def _ny_orb_path_m15() -> list[Candle]:
    """60-minute NY opening range on 14 Jan 2026 with high 2010 and low 1995."""
    return [
        _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2005, low=1999, c=2002),
        _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2002, h=2010, low=2000, c=2004),
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2004, h=2006, low=2001, c=2003),
        _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=2003, h=2004, low=1995, c=1998),
    ]


def _ny_orb_path_m1() -> list[Candle]:
    bars: list[Candle] = []
    start = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    for i in range(60):
        open_ts = start + timedelta(minutes=i)
        ts = open_ts + timedelta(minutes=1)
        open_px = 2000.0
        high, low, close = 2001.0, 1999.0, 2000.0
        if open_ts == datetime(2026, 1, 14, 13, 22, tzinfo=UTC):
            high = 2010.0
            close = 2004.0
        if open_ts == datetime(2026, 1, 14, 13, 47, tzinfo=UTC):
            low = 1995.0
            close = 1998.0
        if i == 59:
            close = 1998.0
            low = min(low, close)
        bars.append(_bar(ts, o=open_px, h=high, low=low, c=close))
    return bars


def test_h4_style_drift_is_rejected() -> None:
    """Broker-style H4 (opens 01:00 UTC) never aligns with the cash opens; skip all."""
    engine = _engine(
        ["tokyo", "london", "new_york"],
        timeframe_minutes=240,
        orb_minutes=240,
        entry_delay_minutes=15,
        anchor_tolerance_minutes=15,
    )
    first_open = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    for i in range(18):
        open_ts = first_open + timedelta(hours=4 * i)
        ts = open_ts + timedelta(hours=4)
        engine.step(_bar(ts, o=2000, h=2010, low=1990, c=2005))
    assert not any(event.kind == "signal" for event in engine.events)
    assert engine.pairs == []
    assert any(event.kind == "signal_skipped_anchor_drift" for event in engine.events)
    report = engine.report("XAUUSD", Timeframe.H4, "local")
    assert all(row.signal_count == 0 for row in report.session_anchor_stats)
    assert sum(row.skip_count for row in report.session_anchor_stats) >= 3


def test_orb_window_independent_of_bar_size() -> None:
    m15 = _engine(["new_york"], timeframe_minutes=15, orb_minutes=60, entry_delay_minutes=15)
    m1 = _engine(["new_york"], timeframe_minutes=1, orb_minutes=60, entry_delay_minutes=15)
    pre = _bar(datetime(2026, 1, 14, 12, 45, tzinfo=UTC), o=1999, h=2000, low=1998, c=2000)
    m15.step(pre)
    for bar in _ny_orb_path_m15():
        m15.step(bar)
    m1.step(pre)
    for bar in _ny_orb_path_m1():
        m1.step(bar)
    assert "new_york" in m15.pending
    assert "new_york" in m1.pending
    assert m15.pending["new_york"].range_price == pytest.approx(15.0)
    assert m1.pending["new_york"].range_price == pytest.approx(15.0)


def test_entry_delay_is_time_based_not_bar_based() -> None:
    engine = _engine(
        ["new_york"],
        timeframe_minutes=15,
        orb_minutes=15,
        entry_delay_minutes=60,
    )
    sequence = [
        _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000),
        _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008),
        _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010),
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2010, h=2011, low=2009, c=2010),
        _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=2011, h=2012, low=2010, c=2011),
        _bar(datetime(2026, 1, 14, 14, 15, tzinfo=UTC), o=2012, h=2013, low=2011, c=2012),
    ]
    for bar in sequence[:-1]:
        engine.step(bar)
    assert engine.pairs == []
    assert "new_york" in engine.pending
    engine.step(sequence[-1])
    assert len(engine.pairs) == 1
    assert engine.pairs[0].entry == 2012.0


def test_report_exposes_anchor_drift_p50_and_max_per_session() -> None:
    engine = _engine(["new_york"])
    pre = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
    signal = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008)
    fill = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010)
    engine.step(pre)
    engine.step(signal)
    engine.step(fill)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.orb_minutes == 15
    assert report.entry_delay_minutes == 15
    assert report.anchor_tolerance_minutes == 15
    ny = next(row for row in report.session_anchor_stats if row.session == "new_york")
    assert ny.anchor_drift_p50 == pytest.approx(0.0)
    assert ny.anchor_drift_max == pytest.approx(0.0)
    assert ny.anchor_drift_minutes == pytest.approx([0.0])
    assert ny.signal_count == 1
    assert ny.skip_count == 0


def test_unlocked_survivor_stop_is_processed() -> None:
    """Restored unlocked pair with one leg already closed must still honor the remaining stop."""
    engine = _engine(["new_york"])
    engine.prev_in_session["new_york"] = True
    pair = Pair(
        id="new_york:restored-survivor",
        session="new_york",
        entry=2000,
        sl_dist=4,
        long_sl=1996,
        long_tp=2012,
        short_sl=2004,
        short_tp=1988,
        primary_side="long",
        long_open=False,
        short_open=True,
        locked=False,
        entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        long_entry=2000,
        short_entry=2000,
    )
    engine.pairs.append(pair)
    engine.step(_bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2000, h=2005, low=1999, c=2003))
    assert pair.short_open is False
    assert pair.locked is False
    shorts = [trade for trade in engine.trades if trade.side == "short"]
    assert len(shorts) == 1
    assert shorts[0].exit == 2004.0


def test_unlocked_survivor_stop_with_rr_below_one() -> None:
    engine = _engine(["new_york"], rr=0.5)
    engine.prev_in_session["new_york"] = True
    pair = Pair(
        id="new_york:rr-below-one",
        session="new_york",
        entry=2000,
        sl_dist=10,
        long_sl=1990,
        long_tp=2005,
        short_sl=2010,
        short_tp=1995,
        primary_side="short",
        long_open=True,
        short_open=False,
        locked=False,
        entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        long_entry=2000,
        short_entry=2000,
    )
    engine.pairs.append(pair)
    engine.step(_bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2000, h=2002, low=1988, c=1992))
    assert pair.long_open is False
    longs = [trade for trade in engine.trades if trade.side == "long"]
    assert len(longs) == 1
    assert longs[0].exit == 1990.0


def test_mid_session_start_does_not_arm_spurious_signal() -> None:
    """Date-ranged backtests that begin inside NY must not treat the first bar as the open."""
    engine = _engine(["new_york"])
    mid = _bar(datetime(2026, 1, 14, 15, 0, tzinfo=UTC), o=2000, h=2010, low=1990, c=2008)
    nxt = _bar(datetime(2026, 1, 14, 15, 15, tzinfo=UTC), o=2008, h=2012, low=2007, c=2010)
    engine.run([mid, nxt])
    assert engine.pending == {}
    assert engine.pairs == []
    assert not any(event.kind == "signal" for event in engine.events)
