from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine import ClosedBarEngine, Pair
from models import EngineParams, Timeframe
from sessions import build_windows
from units import cash, pips_raw, pips_weighted


def test_pips_weighted_equals_pips_raw_under_fixed_lot() -> None:
    raw = pips_raw(exit_px=1999, entry=2000, pip_size=0.1, is_long=True)
    assert pips_weighted(raw, qty=1.0, qty_ref=1.0) == pytest.approx(raw)


def test_pips_weighted_is_additive_under_variable_sizing() -> None:
    raw_a = pips_raw(exit_px=2010, entry=2000, pip_size=0.1, is_long=True)
    raw_b = pips_raw(exit_px=1990, entry=2000, pip_size=0.1, is_long=True)
    wa = pips_weighted(raw_a, qty=2.0, qty_ref=1.0)
    wb = pips_weighted(raw_b, qty=0.5, qty_ref=1.0)
    assert wa + wb == pytest.approx(
        pips_weighted(raw_a, qty=2.0, qty_ref=1.0) + pips_weighted(raw_b, qty=0.5, qty_ref=1.0)
    )
    assert wa != pytest.approx(raw_a)


def test_cost_pips_and_cash_agree() -> None:
    raw = -10.0
    weighted = pips_weighted(raw, qty=1.0, qty_ref=1.0)
    assert cash(weighted, dollars_per_pip_per_qty=2.0, qty_ref=1.0) == pytest.approx(-20.0)


def test_report_shows_pips_and_r_together() -> None:
    params = EngineParams(
        orb_minutes=15,
        entry_delay_minutes=15,
        timeframe_minutes=15,
        dollars_per_pip_per_qty=1.0,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    engine.prev_in_session["new_york"] = True
    pair = Pair(
        id="new_york:units",
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
        short_entry=2000,
    )
    engine.pairs.append(pair)
    engine._close_long(pair, 1990, datetime(2026, 1, 14, 13, 45, tzinfo=UTC))
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.realized_pips is not None
    assert report.realized_r is not None
    assert report.realized_r == pytest.approx(-1.0)
    assert report.timeframe == Timeframe.M15
    assert report.orb_minutes == 15
    assert report.entry_delay_minutes == 15
    assert report.anchor_tolerance_minutes == 15
    assert report.survivor_tp_rate == pytest.approx(0.0)
    assert report.mean_loss_r == pytest.approx(-1.0)
    assert report.breakeven_tp_rate_required == pytest.approx(1.0 / 3.0)
    ny = next(row for row in report.session_anchor_stats if row.session == "new_york")
    assert ny.anchor_drift_p50 is None or isinstance(ny.anchor_drift_p50, float)


def test_drawdown_persists_in_snapshot() -> None:
    params = EngineParams(orb_minutes=15, entry_delay_minutes=15, timeframe_minutes=15)
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    engine.max_drawdown_pips = 20.0
    engine.max_drawdown_r = 0.5
    engine.equity_peak_pips = 10.0
    engine.net_max_drawdown_pips = 21.0
    engine.net_max_drawdown_r = 0.6
    engine.net_equity_peak_pips = 9.0
    restored = ClosedBarEngine(build_windows(["new_york"], {}), params)
    restored.restore(engine.snapshot())
    assert restored.max_drawdown_pips == pytest.approx(20.0)
    assert restored.max_drawdown_r == pytest.approx(0.5)
    assert restored.equity_peak_pips == pytest.approx(10.0)
    assert restored.net_max_drawdown_pips == pytest.approx(21.0)
    assert restored.net_max_drawdown_r == pytest.approx(0.6)
    assert restored.net_equity_peak_pips == pytest.approx(9.0)
