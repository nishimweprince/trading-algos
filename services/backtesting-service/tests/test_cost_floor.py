"""Cost-derived minimum-stop multiples. Incumbent default remains disabled (0)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtesting_service.config import Settings
from backtesting_service.costs import (
    CostSchedule,
    cost_derived_min_stop_pips,
    effective_min_stop_pips,
)
from backtesting_service.engine import ClosedBarEngine
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
    params = EngineParams(
        pip_size=0.1,
        stop_mode=str(kwargs.get("stop_mode", "bar_range")),
        sl_mult=2.0,
        fixed_stop_pips=float(kwargs.get("fixed_stop_pips", 0.0)),  # type: ignore[arg-type]
        min_stop_pips=float(kwargs.get("min_stop_pips", 0.0)),  # type: ignore[arg-type]
        min_stop_cost_mult=float(kwargs.get("min_stop_cost_mult", 0.0)),  # type: ignore[arg-type]
        orb_minutes=15,
        timeframe_minutes=15,
        entry_delay_minutes=15,
        anchor_tolerance_minutes=15,
        cost_model=str(kwargs.get("cost_model", "per_session")),
        spread_pips_per_side=float(kwargs.get("spread_pips_per_side", 0.0)),  # type: ignore[arg-type]
        slippage_pips_per_side=float(kwargs.get("slippage_pips_per_side", 0.0)),  # type: ignore[arg-type]
        commission_pips_per_side=float(kwargs.get("commission_pips_per_side", 0.0)),  # type: ignore[arg-type]
    )
    return ClosedBarEngine(build_windows(["new_york"], {}), params)


def _open_ny_stop(**params: object) -> float:
    engine = _engine(**params)
    engine.step(_bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000))
    engine.step(_bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2010))
    engine.step(_bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010))
    return engine.pairs[0].sl_dist


def test_cost_derived_floor_is_all_in_execution_times_multiple() -> None:
    schedule = CostSchedule(
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        commission_pips_per_side=0.0,
        swap_long_pips_per_rollover=9.9,
    )
    assert cost_derived_min_stop_pips(schedule, 0) == 0.0
    assert cost_derived_min_stop_pips(schedule, 2) == pytest.approx(5.0)
    assert cost_derived_min_stop_pips(schedule, 3) == pytest.approx(7.5)
    assert effective_min_stop_pips(
        min_stop_pips=6.0, min_stop_cost_mult=2, schedule=schedule
    ) == pytest.approx(6.0)
    assert effective_min_stop_pips(
        min_stop_pips=4.0, min_stop_cost_mult=2, schedule=schedule
    ) == pytest.approx(5.0)


def test_incumbent_cost_mult_zero_does_not_change_bar_range_stop() -> None:
    assert _open_ny_stop() == pytest.approx(20.0)
    assert _open_ny_stop(min_stop_cost_mult=0) == pytest.approx(20.0)


def test_cost_multiple_floors_a_narrow_fixed_stop() -> None:
    sl_dist = _open_ny_stop(
        stop_mode="fixed_pips",
        fixed_stop_pips=1,
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        min_stop_cost_mult=2,
    )
    assert sl_dist == pytest.approx(0.5)


def test_cost_multiple_does_not_shrink_a_wider_stop() -> None:
    sl_dist = _open_ny_stop(
        stop_mode="fixed_pips",
        fixed_stop_pips=150,
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        min_stop_cost_mult=2,
    )
    assert sl_dist == pytest.approx(15.0)


def test_disabled_cost_model_yields_a_zero_derived_floor() -> None:
    sl_dist = _open_ny_stop(
        stop_mode="fixed_pips",
        fixed_stop_pips=1,
        cost_model="none",
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        min_stop_cost_mult=2,
    )
    assert sl_dist == pytest.approx(0.1)


def test_report_and_config_serialize_the_cost_floor() -> None:
    engine = _engine(
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        min_stop_cost_mult=3,
    )
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.min_stop_cost_mult == pytest.approx(3.0)
    assert report.report_header.derived_min_stop_pips == pytest.approx(7.5)
    dumped = EngineParams.model_validate(engine.params.model_dump())
    assert dumped.min_stop_cost_mult == pytest.approx(3.0)
    settings = Settings(
        spread_pips_per_side=2.0,
        slippage_pips_per_side=0.5,
        min_stop_cost_mult=2,
    )
    assert settings.engine_params().min_stop_cost_mult == pytest.approx(2.0)
    restored = ClosedBarEngine(build_windows(["new_york"], {}), dumped)
    restored.restore(engine.snapshot())
    assert restored.params.min_stop_cost_mult == pytest.approx(3.0)


def test_incumbent_report_leaves_derived_floor_unset() -> None:
    engine = _engine()
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.min_stop_cost_mult == pytest.approx(0.0)
    assert report.report_header.derived_min_stop_pips is None
    assert report.report_header.min_stop_pips == pytest.approx(0.0)
