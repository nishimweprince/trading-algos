"""Candles for backtests and paper trading: a local JSONL cache, plus
market-data-service for anything the cache does not hold.

Backtests read the cache and never touch the network; paper trading and
``--seed`` fetch. Whichever broker plugin serves ``MARKET_DATA_MARKET`` on the
service, bars arrive closed and stamped at their UTC interval end, so nothing
here knows about MT5 server time, forming bars or broker symbol suffixes.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from ta_clients import JsonlCandleCache, MarketDataClient
from ta_contracts import Candle, Timeframe

from .config import Settings


class CandleStore:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None) -> None:
        self._cache = JsonlCandleCache(settings.local_candles_path)
        self._remote = (
            None
            if client is None
            else MarketDataClient(
                settings.market_data_url,
                market=settings.market_data_market,
                api_key=(
                    settings.market_data_api_key.get_secret_value()
                    if settings.market_data_api_key is not None
                    else None
                ),
                client=client,
            )
        )

    # --- local cache --------------------------------------------------------

    def local_path(self, symbol: str, timeframe: Timeframe):
        return self._cache.path(symbol, timeframe)

    def local_exists(self, symbol: str, timeframe: Timeframe) -> bool:
        return self._cache.exists(symbol, timeframe)

    def load_local(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        count: int | None = None,
    ) -> list[Candle]:
        return self._cache.load(
            symbol, timeframe, date_from=date_from, date_to=date_to, count=count
        )

    def write_local(self, symbol: str, timeframe: Timeframe, candles: list[Candle]):
        return self._cache.write(symbol, timeframe, candles)

    # --- market-data-service ------------------------------------------------

    async def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        return await self._client().candles(symbol, timeframe, count=count, to=to)

    async def fetch_range(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[Candle]:
        return await self._client().candles_range(
            symbol, timeframe, date_from=date_from, date_to=date_to
        )

    async def gateway_ready(self) -> tuple[bool, str]:
        return await self._client().ready()

    def _client(self) -> MarketDataClient:
        if self._remote is None:
            raise RuntimeError("this CandleStore was built without an HTTP client")
        return self._remote


def create_candle_store(settings: Settings, http: httpx.AsyncClient) -> CandleStore:
    return CandleStore(settings, http)
