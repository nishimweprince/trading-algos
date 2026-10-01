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
from .market_data import MarketDataFactory, MarketDataProvider, ProviderCapabilities

__all__ = [
    "EXECUTION_GROUP",
    "MARKET_DATA_GROUP",
    "MarketDataFactory",
    "MarketDataHub",
    "MarketDataProvider",
    "PluginError",
    "ProviderCapabilities",
    "ProviderFactory",
    "Role",
    "StreamEvent",
    "Subscriber",
    "SymbolResolutionError",
    "available",
    "load_providers",
]
