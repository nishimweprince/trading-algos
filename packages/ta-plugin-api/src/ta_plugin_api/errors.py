"""Errors every provider plugin shares.

Broker-specific failures (a cTrader error code, a malformed frame) stay in the
plugin that raises them. What lives here is what a service has to catch without
knowing which plugin is behind it.
"""

from __future__ import annotations

__all__ = ["PluginError", "SymbolResolutionError"]


class PluginError(RuntimeError):
    """A configured provider could not be discovered or loaded.

    Raised at startup and never caught: a service told to run a provider it
    cannot load must refuse to start, not come up without it.
    """


class SymbolResolutionError(ValueError):
    """A configured or requested symbol has no unambiguous broker mapping."""
