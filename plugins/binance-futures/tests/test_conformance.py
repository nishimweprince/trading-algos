"""The plugin API's conformance kit, run against the fake fapi."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import httpx
import pytest
from ta_contracts import Timeframe
from ta_plugin_api.testing import MarketDataConformance

from ta_plugin_binance_futures.market_data import BinanceFuturesMarketData
from ta_plugin_binance_futures.testing import (
    NOW,
    FakeBinanceFutures,
    FakeFuturesStream,
    book_ticker_frame,
    settings,
)


class TestMarketDataConformance(MarketDataConformance):
    @pytest.fixture
    async def provider(self) -> AsyncIterator[BinanceFuturesMarketData]:
        config = settings()
        stream = FakeFuturesStream(
            {
                "public": [
                    [
                        book_ticker_frame("BTCUSDT", "60000.00", "1", "60000.10", "2"),
                        book_ticker_frame("ETHUSDT", "2500.00", "1", "2500.01", "2"),
                    ]
                ]
            }
        )
        http = httpx.AsyncClient(
            base_url=config.binance_futures_rest_url,
            transport=httpx.MockTransport(FakeBinanceFutures().handler),
        )
        provider = BinanceFuturesMarketData(config, http=http, ws_connect=stream, clock=lambda: NOW)
        await provider.start()
        assert await provider.wait_ready(1.0)
        for _ in range(20):
            await asyncio.sleep(0)
        yield provider
        stream.release.set()
        await provider.close()
        await http.aclose()

    @pytest.fixture
    def feed(self) -> None:
        return None

    @pytest.fixture
    def symbol(self) -> str:
        return "BTCUSDT"

    @pytest.fixture
    def timeframe(self) -> Timeframe:
        return Timeframe.M1

    @pytest.fixture
    def now(self) -> datetime:
        return NOW
