"""Strategy registration.

The point of this module is that adding strategy number two is an addition, not
a fork of the service. It is the *seam*, deliberately introduced before the
engine is actually split behind it, so the API contract and the naming settle
while there is still only one strategy to be wrong about.

What is here now:

- ``StrategyPlugin``, the shape a strategy must present.
- ``session_hedge``, the built-in, registered against the engine as it stands.
- Discovery of third-party strategies through the ``ta.strategies`` entry-point
  group, so a strategy can live in its own distribution.

What is deliberately NOT here yet: the engine still owns the hedge-pair and
prop-guard logic directly rather than calling through ``StrategyPlugin.build``.
engine.py is 3,800 lines and its entry, OCO and risk paths are interleaved;
splitting it is sequenced behind the determinism gate (see README) and is not a
one-sitting change. Until that lands, ``build`` returns the engine parameters
and the registry's real job is validating and naming the strategy a request
asks for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol

from pydantic import BaseModel

from .models import EngineParams

ENTRY_POINT_GROUP = "ta.strategies"
DEFAULT_STRATEGY = "session_hedge"


class StrategyPlugin(Protocol):
    """One backtestable strategy."""

    name: str

    def params_model(self) -> type[BaseModel]:
        """The request model this strategy accepts, for validation and OpenAPI."""
        ...

    def build(self, params: BaseModel) -> EngineParams:
        """Turn validated request parameters into engine parameters."""
        ...


@dataclass(frozen=True)
class SimpleStrategy:
    """A plugin defined by a name and a params -> EngineParams function."""

    name: str
    _params_model: type[BaseModel]
    _build: Callable[[BaseModel], EngineParams]

    def params_model(self) -> type[BaseModel]:
        return self._params_model

    def build(self, params: BaseModel) -> EngineParams:
        return self._build(params)


def _session_hedge_build(params: BaseModel) -> EngineParams:
    if isinstance(params, EngineParams):
        return params
    return EngineParams.model_validate(params.model_dump(exclude_none=True))


SESSION_HEDGE = SimpleStrategy(
    name=DEFAULT_STRATEGY,
    _params_model=EngineParams,
    _build=_session_hedge_build,
)

_BUILTINS: dict[str, StrategyPlugin] = {SESSION_HEDGE.name: SESSION_HEDGE}


def _discovered() -> dict[str, StrategyPlugin]:
    """Strategies published by other distributions.

    A plugin that fails to load is skipped rather than taking the service down:
    a broken third-party strategy must not stop the built-in one backtesting.
    """
    found: dict[str, StrategyPlugin] = {}
    try:
        points = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - importlib.metadata surface varies by env
        return found
    for point in points:
        try:
            plugin = point.load()
        except Exception:  # noqa: BLE001 - see docstring
            continue
        name = getattr(plugin, "name", point.name)
        found[name] = plugin
    return found


def available() -> dict[str, StrategyPlugin]:
    """Built-ins first, so a third party cannot shadow session_hedge."""
    return {**_discovered(), **_BUILTINS}


def get(name: str | None) -> StrategyPlugin:
    strategies = available()
    key = name or DEFAULT_STRATEGY
    if key not in strategies:
        raise KeyError(f"unknown strategy {key!r}; available: {', '.join(sorted(strategies))}")
    return strategies[key]
