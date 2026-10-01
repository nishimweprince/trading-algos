"""The plugin API's conformance kit, run against recorded Binance responses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from ta_contracts import Timeframe
from ta_plugin_api.testing import MarketDataConformance

from ta_plugin_binance.market_data import BinanceMarketData
from ta_plugin_binance.testing import NOW, FakeBinance, FakeStream, book_ticker_frames


class TestMarketDataConformance(MarketDataConformance):
    @pytest.fixture
    async def provider(self) -> AsyncIterator[BinanceMarketData]:
        settings = SimpleNamespace(
            binance_rest_url="https://api.binance.test",
            binance_ws_url="wss://stream.binance.test:9443",
            binance_symbols=("BTCUSDT", "ETHUSDT"),
            binance_request_weight_per_minute=4800,
            binance_timeout_seconds=5.0,
            binance_reconnect_max_backoff_seconds=0.01,
            subscriber_queue_size=16,
        )
        stream = FakeStream(book_ticker_frames())
        http = httpx.AsyncClient(
            base_url=settings.binance_rest_url,
            transport=httpx.MockTransport(FakeBinance().handler),
        )
        provider = BinanceMarketData(settings, http=http, ws_connect=stream, clock=lambda: NOW)
        await provider.start()
        assert await provider.wait_ready(1.0)
        for _ in range(20):  # let the fake stream deliver its frames
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
