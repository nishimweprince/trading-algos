from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtesting_service.engine import ClosedBarEngine, Pair
from backtesting_service.models import EngineParams, Timeframe
from backtesting_service.sessions import build_windows
from backtesting_service.units import cash, conversion_factor, pips_raw, pips_weighted


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


def test_conversion_factor_is_one_for_pips() -> None:
    assert conversion_factor(unit="pips", dollars_per_pip_per_qty=None, qty_ref=1) == 1.0
    # A dollar rate is irrelevant while the unit is pips.
    assert conversion_factor(unit="pips", dollars_per_pip_per_qty=10, qty_ref=2) == 1.0


def test_conversion_factor_scales_by_the_cash_value_of_one_pip() -> None:
    assert conversion_factor(unit="dollars", dollars_per_pip_per_qty=10, qty_ref=1) == 10.0
    assert conversion_factor(unit="dollars", dollars_per_pip_per_qty=10, qty_ref=2) == 20.0
    assert conversion_factor(unit="dollars", dollars_per_pip_per_qty=2.5, qty_ref=1) == 2.5


def test_dollar_reporting_without_a_rate_is_an_error_not_a_zero() -> None:
    with pytest.raises(ValueError, match="dollars-per-pip"):
        conversion_factor(unit="dollars", dollars_per_pip_per_qty=None, qty_ref=1)


def test_conversion_agrees_with_the_cash_helper() -> None:
    factor = conversion_factor(unit="dollars", dollars_per_pip_per_qty=10, qty_ref=2)
    assert 137.5 * factor == cash(137.5, dollars_per_pip_per_qty=10, qty_ref=2)


def _view_engine(unit: str, rate: float | None) -> ClosedBarEngine:
    params = EngineParams(
        performance_unit=unit,
        dollars_per_pip_per_qty=rate,
        cost_model="per_session",
        spread_pips_per_side=1,
        time_exit_mode="max_age",
        max_age_hours=24,
        one_open_per_session=False,
        max_concurrent_structures=0,
        max_open_risk_pct=0,
    )
    from pathlib import Path

    from backtesting_service.models import Candle
    from backtesting_service.sessions import build_windows

    fixture = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
    candles = [
        Candle.model_validate_json(line)
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    engine = ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)
    engine.run(candles)
    return engine


def test_pip_mode_performance_view_mirrors_the_pip_fields() -> None:
    report = _view_engine("pips", None).report("XAUUSD", Timeframe.M15, "local")
    view = report.performance

    assert view.unit == "pips"
    assert view.unit_label == "pips"
    assert view.conversion_factor == 1.0
    assert view.dollars_per_pip_per_qty is None
    assert view.gross_equity == pytest.approx(report.gross_equity_pips)
    assert view.net_equity == pytest.approx(report.net_equity_pips)
    assert view.gross_realized == pytest.approx(report.gross_realized_pips)
    assert view.net_realized == pytest.approx(report.net_realized_pips)
    assert view.execution_cost == pytest.approx(report.execution_cost_pips)
    assert view.gross_max_drawdown == pytest.approx(report.gross_max_drawdown_pips)
    assert view.net_max_drawdown == pytest.approx(report.net_max_drawdown_pips)
    assert view.breakeven_per_completed_side == pytest.approx(report.breakeven_pips_per_side)


def test_dollar_mode_scales_every_additive_metric_by_one_factor() -> None:
    pips = _view_engine("pips", None).report("XAUUSD", Timeframe.M15, "local").performance
    dollars = _view_engine("dollars", 10).report("XAUUSD", Timeframe.M15, "local").performance

    assert dollars.unit == "dollars"
    assert dollars.unit_label == "$"
    assert dollars.conversion_factor == pytest.approx(10.0)
    for field in (
        "gross_realized",
        "net_realized",
        "gross_unrealized",
        "net_unrealized",
        "gross_equity",
        "net_equity",
        "equity_cost",
        "execution_cost",
        "financing_cost",
        "max_drawdown",
        "gross_max_drawdown",
        "net_max_drawdown",
        "configured_spread_per_side",
        "configured_execution_cost_per_side",
    ):
        assert getattr(dollars, field) == pytest.approx(getattr(pips, field) * 10.0), field
    assert dollars.breakeven_per_completed_side == pytest.approx(
        pips.breakeven_per_completed_side * 10.0
    )


def test_performance_view_reconciles_gross_cost_and_net() -> None:
    view = _view_engine("dollars", 4).report("XAUUSD", Timeframe.M15, "local").performance

    assert view.gross_equity - view.equity_cost == pytest.approx(view.net_equity)
    assert view.gross_realized - view.realized_cost == pytest.approx(view.net_realized)
    assert view.gross_unrealized - view.unrealized_cost == pytest.approx(view.net_unrealized)
    assert view.equity_cost == pytest.approx(view.realized_cost + view.unrealized_cost)
