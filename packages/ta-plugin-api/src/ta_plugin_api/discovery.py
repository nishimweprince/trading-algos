"""Find provider plugins through entry points, failing closed.

A plugin is a distribution that publishes a ``ProviderFactory`` under one or
both groups::

    [project.entry-points."ta.execution"]
    ctrader = "ta_plugin_ctrader:FACTORY"

This is the same mechanism as ``ta.strategies`` in backtesting-service, with
the opposite failure policy. A strategy that fails to import is skipped so the
built-ins keep working; a *broker* that a host is configured to run and cannot
load must stop the service from starting. Coming up without it would serve
health checks while every order and quote for that broker fails.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Protocol, runtime_checkable

from .errors import PluginError

__all__ = [
    "EXECUTION_GROUP",
    "MARKET_DATA_GROUP",
    "ProviderFactory",
    "Role",
    "available",
    "load_providers",
]

MARKET_DATA_GROUP = "ta.market_data"
EXECUTION_GROUP = "ta.execution"


class Role(StrEnum):
    MARKET_DATA = MARKET_DATA_GROUP
    EXECUTION = EXECUTION_GROUP


@runtime_checkable
class ProviderFactory(Protocol):
    """What an entry point resolves to.

    ``name`` must equal the entry-point name, so the string an operator writes
    in configuration is the one the plugin answers to.
    """

    name: str

    def missing_settings(self, settings: Any) -> list[str]:
        """Environment names this provider requires and ``settings`` lacks.

        Lets a service validate configuration for exactly the providers it was
        told to run, so a cTrader host never needs an MT5 terminal path.
        """
        ...


def _points(group: str) -> dict[str, EntryPoint]:
    found: dict[str, EntryPoint] = {}
    for point in entry_points(group=group):
        existing = found.get(point.name)
        if existing is not None and existing.value != point.value:
            raise PluginError(
                f"provider {point.name!r} is published twice in {group}: "
                f"{existing.value} and {point.value}"
            )
        found[point.name] = point
    return found


def available(group: str) -> frozenset[str]:
    """Provider names published in ``group``, without importing any of them."""
    return frozenset(_points(group))


def load_providers(group: str, names: Iterable[str]) -> dict[str, ProviderFactory]:
    """Load exactly ``names`` from ``group``, or raise ``PluginError``."""
    points = _points(group)
    loaded: dict[str, ProviderFactory] = {}
    for name in names:
        if name in loaded:
            continue
        point = points.get(name)
        if point is None:
            known = ", ".join(sorted(points)) or "none installed"
            raise PluginError(f"no provider {name!r} in {group}; available: {known}")
        try:
            factory = point.load()
        except Exception as exc:
            raise PluginError(f"provider {name!r} in {group} failed to load: {exc}") from exc
        if not isinstance(factory, ProviderFactory):
            raise PluginError(f"provider {name!r} in {group} is not a ProviderFactory")
        if factory.name != name:
            raise PluginError(
                f"provider entry point {name!r} in {group} resolves to {factory.name!r}"
            )
        loaded[name] = factory
    return loaded
