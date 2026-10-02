"""The entry point services discover, plus the plugin's own stream interface.

``market_data`` satisfies the platform contract. ``streams`` and ``account``
are this plugin's typed extras (decision (b) in ofi-scalper-plan.md §2.4): a
service reaches them through the factory it got from ``load_providers``, so it
still never constructs a provider class itself.
"""

from __future__ import annotations

from typing import Any

from .account import AccountReader
from .execution import BinanceFuturesExecution
from .instruments import PROVIDER_NAME
from .market_data import BinanceFuturesMarketData
from .quotes import TouchQuotes
from .rest import FapiRest
from .streams import FuturesStreams, RawSink

__all__ = [
    "EXECUTION_FACTORY",
    "FACTORY",
    "BinanceFuturesExecutionFactory",
    "BinanceFuturesFactory",
]


class BinanceFuturesFactory:
    name = PROVIDER_NAME

    def missing_settings(self, settings: Any) -> list[str]:
        return [] if settings.binance_futures_symbols else ["BINANCE_FUTURES_SYMBOLS"]

    def market_data(self, settings: Any) -> BinanceFuturesMarketData:
        """Public quotes and candles for BINANCE_FUTURES_SYMBOLS."""
        return BinanceFuturesMarketData(settings)

    def rest(self, settings: Any) -> FapiRest:
        """One REST client (and one weight budget) to share across the extras."""
        return FapiRest(settings)

    def streams(
        self, settings: Any, *, rest: FapiRest | None = None, on_raw: RawSink | None = None
    ) -> FuturesStreams:
        """Depth with verified local books, book ticker, trades, funding, liquidations."""
        return FuturesStreams(settings, rest=rest, on_raw=on_raw)

    def account(self, settings: Any, *, rest: FapiRest | None = None) -> AccountReader:
        """Read-only signed reads; ``available`` is False without a key."""
        sapi_url = getattr(settings, "binance_sapi_url", "")
        sapi = FapiRest(settings, base_url=sapi_url) if sapi_url else None
        return AccountReader(rest or FapiRest(settings), sapi=sapi)

    def quotes(self, settings: Any, ws_url: str) -> TouchQuotes:
        """Best bid/ask only, from another venue's streams (e.g. demo trading)."""
        return TouchQuotes(
            ws_url,
            settings.binance_futures_symbols,
            max_backoff_seconds=settings.binance_futures_reconnect_max_backoff_seconds,
        )


FACTORY = BinanceFuturesFactory()


class BinanceFuturesExecutionFactory:
    """Order entry, published under ``ta.execution`` as ``binance_futures``.

    A separate object from ``FACTORY`` because its ``missing_settings`` requires
    the trading key, which market data must never need.
    """

    name = PROVIDER_NAME

    def missing_settings(self, settings: Any) -> list[str]:
        missing = [] if settings.binance_futures_symbols else ["BINANCE_FUTURES_SYMBOLS"]
        if getattr(settings, "binance_futures_trading_api_key", None) is None:
            missing.append("BINANCE_FUTURES_TRADING_API_KEY")
        if getattr(settings, "binance_futures_trading_api_secret", None) is None:
            missing.append("BINANCE_FUTURES_TRADING_API_SECRET")
        return missing

    def execution(self, settings: Any, **overrides: Any) -> BinanceFuturesExecution:
        return BinanceFuturesExecution(settings, **overrides)


EXECUTION_FACTORY = BinanceFuturesExecutionFactory()
