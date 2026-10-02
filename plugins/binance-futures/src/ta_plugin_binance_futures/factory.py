"""The entry point services discover, plus the plugin's own stream interface.

``market_data`` satisfies the platform contract. ``streams`` and ``account``
are this plugin's typed extras (decision (b) in ofi-scalper-plan.md §2.4): a
service reaches them through the factory it got from ``load_providers``, so it
still never constructs a provider class itself.
"""

from __future__ import annotations

from typing import Any

from .account import AccountReader
from .instruments import PROVIDER_NAME
from .market_data import BinanceFuturesMarketData
from .rest import FapiRest
from .streams import FuturesStreams, RawSink

__all__ = ["FACTORY", "BinanceFuturesFactory"]


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


FACTORY = BinanceFuturesFactory()
