"""The contract between services and the broker plugins under plugins/. See README.md."""

from .discovery import (
    EXECUTION_GROUP,
    MARKET_DATA_GROUP,
    ProviderFactory,
    Role,
    available,
    load_providers,
)
from .errors import PluginError, SymbolResolutionError
from .hub import MarketDataHub, StreamEvent, Subscriber

__all__ = [
    "EXECUTION_GROUP",
    "MARKET_DATA_GROUP",
    "MarketDataHub",
    "PluginError",
    "ProviderFactory",
    "Role",
    "StreamEvent",
    "Subscriber",
    "SymbolResolutionError",
    "available",
    "load_providers",
]
