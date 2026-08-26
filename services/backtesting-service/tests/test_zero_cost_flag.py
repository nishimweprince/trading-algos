"""A costless run must announce itself on the report header."""

from __future__ import annotations

from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import EngineParams, Timeframe
from backtesting_service.sessions import build_windows


def _engine(**overrides: object) -> ClosedBarEngine:
    params = EngineParams.model_validate(
        EngineParams(
            entry_mode="oco_bracket",
            cost_model="per_session",
            timeframe_minutes=60,
            orb_minutes=60,
        ).model_dump()
        | overrides
    )
    return ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)


def _header(engine: ClosedBarEngine):
    return engine.report("XAUUSD", Timeframe.H1, "local").report_header


def test_all_zero_costs_are_flagged() -> None:
    assert _header(_engine()).costs_are_zero is True


def test_cost_model_none_is_flagged_even_with_a_schedule() -> None:
    engine = _engine(cost_model="none", spread_pips_per_side=1.5)
    assert _header(engine).costs_are_zero is True


def test_any_execution_cost_clears_the_flag() -> None:
    assert _header(_engine(spread_pips_per_side=1.5)).costs_are_zero is False
    assert _header(_engine(commission_pips_per_side=0.35)).costs_are_zero is False
    assert _header(_engine(slippage_pips_per_side=1.0)).costs_are_zero is False


def test_swap_only_clears_the_flag() -> None:
    assert _header(_engine(swap_long_pips_per_rollover=2.0)).costs_are_zero is False


def test_a_session_override_alone_clears_the_flag() -> None:
    engine = _engine(session_cost_overrides={"tokyo": {"spread_pips_per_side": 2.5}})
    assert _header(engine).costs_are_zero is False


def test_shipped_env_is_not_costless() -> None:
    from backtesting_service.config import load_settings

    params = load_settings().engine_params()
    engine = ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)
    assert _header(engine).costs_are_zero is False, ".env must ship real broker costs"
