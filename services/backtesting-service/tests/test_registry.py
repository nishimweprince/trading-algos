"""The strategy seam.

These pin the contract that makes strategy number two an addition rather than a
fork, before the engine is actually split behind it.
"""

from __future__ import annotations

import pytest

from backtesting_service import registry
from backtesting_service.models import BacktestRequest, EngineParams


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


def test_backtest_request_defaults_strategy_to_none() -> None:
    assert BacktestRequest().strategy is None
