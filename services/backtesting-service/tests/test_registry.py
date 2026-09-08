"""The strategy seam.

These pin the contract that makes strategy number two an addition rather than a
fork: the plugin owns both the parameter assembly and the entry staging, and
the engine binds to what the registry resolved.
"""

from __future__ import annotations

import pytest

from backtesting_service import registry
from backtesting_service.models import BacktestRequest, EngineParams
from backtesting_service.strategies import session_hedge
from backtesting_service.strategies.facade import StrategyExecution


def test_session_hedge_is_registered() -> None:
    assert registry.DEFAULT_STRATEGY in registry.available()


def test_unset_strategy_resolves_to_session_hedge() -> None:
    """Every existing caller and stored request omits the field."""
    assert registry.get(None).name == registry.DEFAULT_STRATEGY


def test_unknown_strategy_is_rejected_by_name() -> None:
    with pytest.raises(KeyError, match="unknown strategy"):
        registry.get("no_such_strategy")


def test_unknown_strategy_error_lists_what_is_available() -> None:
    with pytest.raises(KeyError, match="session_hedge"):
        registry.get("no_such_strategy")


def test_builtin_cannot_be_shadowed_by_a_plugin(monkeypatch) -> None:
    """A third-party distribution must not be able to replace session_hedge."""
    impostor = registry.SimpleStrategy(
        name=registry.DEFAULT_STRATEGY,
        _params_model=EngineParams,
        _build=lambda params: EngineParams(),
    )
    monkeypatch.setattr(registry, "_discovered", lambda: {registry.DEFAULT_STRATEGY: impostor})
    assert registry.get(registry.DEFAULT_STRATEGY) is registry.SESSION_HEDGE


def test_a_broken_plugin_does_not_break_discovery(monkeypatch) -> None:
    """A third-party strategy that fails to import must not stop the built-in."""

    class Boom:
        name = "boom"

        def load(self):
            raise ImportError("no")

    monkeypatch.setattr(registry, "entry_points", lambda group: [Boom()])
    assert registry.get(None).name == registry.DEFAULT_STRATEGY


def test_session_hedge_build_returns_engine_params() -> None:
    params = registry.get(None).build(EngineParams())
    assert isinstance(params, EngineParams)


def test_session_hedge_build_assembles_params_behind_the_seam() -> None:
    """Request overrides reach EngineParams through ``build``, not the API layer."""
    from backtesting_service.config import Settings

    base = Settings().engine_params()
    params = registry.get(None).build(
        registry.StrategyBuildInputs(base=base, body=BacktestRequest(), timeframe_minutes=60)
    )
    assert params == base.model_copy(update={"timeframe_minutes": 60})


def test_backtest_request_defaults_strategy_to_none() -> None:
    assert BacktestRequest().strategy is None


def test_session_hedge_execution_is_the_staging_module() -> None:
    """The built-in owns both halves: parameters and staging come from one plugin."""
    assert registry.execution_for(registry.SESSION_HEDGE) is session_hedge


def test_a_parameters_only_plugin_falls_back_to_the_builtin_staging() -> None:
    """Publishing a strategy that only reshapes parameters stays a two-method job."""
    params_only = registry.SimpleStrategy(
        name="params_only",
        _params_model=EngineParams,
        _build=lambda params: EngineParams(),
    )
    assert registry.execution_for(params_only) is session_hedge


def test_a_plugin_can_supply_its_own_staging() -> None:
    """The point of the execution half: staging is chosen, not hardcoded."""
    other = registry.SimpleStrategy(
        name="other",
        _params_model=EngineParams,
        _build=lambda params: EngineParams(),
        _execution=session_hedge,
    )
    assert registry.execution_for(other) is session_hedge


def test_session_hedge_satisfies_the_execution_protocol() -> None:
    """If the engine renames a delegate, this fails before a strategy silently drifts."""
    for name in (
        "stage_synthetic_order",
        "stage_oco_bracket",
        "stage_fractional_contingent",
        "scale_fractional_contingent",
        "stage_contingent_hedges",
        "stage_oco_reentries",
        "open_pair",
    ):
        assert callable(getattr(session_hedge, name)), name
        assert callable(getattr(StrategyExecution, name)), name
