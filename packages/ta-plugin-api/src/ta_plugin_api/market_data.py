"""What a market-data provider plugin implements.

One provider instance serves every *feed* it was configured with. A feed is the
provider's own scope for "which connection answers this": a cTrader account
alias, or ``None`` for a provider with a single connection such as one MT5
terminal. market-data-service maps each market (forex, deriv, crypto) to one
provider and one feed, so a provider never needs to know about markets.

Errors are ``ta_core.ServiceError`` with stable codes, raised by the provider
and passed to the HTTP caller unchanged:

- 422 ``symbol_not_allowed``, ``timeframe_not_supported``, ``count_exceeds_limit``
- 503 ``broker_not_ready``, ``tick_unavailable``, ``candles_unavailable``
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ta_contracts import Candle, InstrumentInfo, MarketQuote, Timeframe

from .discovery import ProviderFactory
from .hub import MarketDataHub

__all__ = [
    "MarketDataFactory",
    "MarketDataProvider",
    "ProviderCapabilities",
]


@dataclass(frozen=True)
class ProviderCapabilities:
    timeframes: tuple[Timeframe, ...]
    streaming: bool
    bid_ask: bool


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    async def start(self) -> None: ...

    async def wait_ready(self, timeout_seconds: float) -> bool: ...

    async def close(self) -> None: ...

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """(ready, details) for the provider's connection, not quote freshness;
        the service judges staleness itself from each feed's hub."""
        ...

    def feeds(self) -> frozenset[str | None]:
        """Every feed this instance can serve. Known before ``start``."""
        ...

    def capabilities(self, feed: str | None) -> ProviderCapabilities: ...

    def instruments(self, feed: str | None) -> list[InstrumentInfo]: ...

    def resolve_symbols(self, feed: str | None, symbols: Iterable[str]) -> frozenset[str]:
        """Canonical names for a stream filter. Raises 422 on any unknown one."""
        ...

    async def quote(self, feed: str | None, symbol: str) -> MarketQuote: ...

    async def candles(
        self,
        feed: str | None,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        """Closed bars only, oldest first, each stamped at its UTC interval end."""
        ...

    def hub(self, feed: str | None) -> MarketDataHub: ...


@runtime_checkable
class MarketDataFactory(ProviderFactory, Protocol):
    """A ``ProviderFactory`` published in ``ta.market_data``."""

    def market_data(self, settings: Any) -> MarketDataProvider: ...
