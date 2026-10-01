from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ta_contracts import Timeframe
from ta_core import ServiceError
from ta_plugin_api import MarketDataProvider
from ta_plugin_api.testing import assert_closed_utc_candles

from ta_plugin_binance import FACTORY
from ta_plugin_binance.limiter import WeightLimiter
from ta_plugin_binance.market_data import BinanceMarketData
from ta_plugin_binance.testing import NOW, FakeBinance, FakeStream, book_ticker_frames


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "binance_rest_url": "https://api.binance.test",
        "binance_ws_url": "wss://stream.binance.test:9443",
        "binance_symbols": ("BTCUSDT", "ETHUSDT"),
        "binance_request_weight_per_minute": 4800,
        "binance_timeout_seconds": 5.0,
        "binance_reconnect_max_backoff_seconds": 0.01,
        "subscriber_queue_size": 16,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def server() -> FakeBinance:
    return FakeBinance()


@pytest.fixture
def stream() -> FakeStream:
    return FakeStream(book_ticker_frames())


@pytest.fixture
async def provider(server: FakeBinance, stream: FakeStream) -> AsyncIterator[BinanceMarketData]:
    http = httpx.AsyncClient(
        base_url="https://api.binance.test", transport=httpx.MockTransport(server.handler)
    )
    provider = BinanceMarketData(_settings(), http=http, ws_connect=stream, clock=lambda: NOW)
    await provider.start()
    assert await provider.wait_ready(1.0)
    for _ in range(20):  # let the fake stream deliver its frames
        await asyncio.sleep(0)
    yield provider
    stream.release.set()
    await provider.close()
    await http.aclose()


async def test_satisfies_the_protocol_and_is_discoverable(provider: BinanceMarketData) -> None:
    assert isinstance(provider, MarketDataProvider)
    assert FACTORY.name == "binance"
    assert FACTORY.missing_settings(_settings(binance_symbols=())) == ["BINANCE_SYMBOLS"]
    assert provider.feeds() == frozenset({None})
    ready, details = provider.readiness()
    assert ready and details["connected"] is True


async def test_instruments_carry_exchange_filters(provider: BinanceMarketData) -> None:
    btc, eth = provider.instruments(None)

    assert (btc.symbol, btc.digits, btc.price_increment) == ("BTCUSDT", 2, 0.01)
    assert (btc.quantity_increment, btc.min_quantity, btc.max_quantity) == (1e-05, 1e-05, 9000.0)
    assert btc.description == "BTC/USDT"
    assert eth.quantity_increment == 0.0001


async def test_stream_quotes_reach_the_hub(provider: BinanceMarketData, stream) -> None:
    quote = await provider.quote(None, "btcusdt")

    assert stream.urls == [
        "wss://stream.binance.test:9443/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
    ]
    assert (quote.bid, quote.ask) == (114251.0, 114251.01)
    assert quote.price == pytest.approx(114251.005)
    assert quote.provider == "binance" and quote.ts == NOW


async def test_candles_are_closed_and_stamped_at_interval_end(
    provider: BinanceMarketData,
) -> None:
    candles = await provider.candles(None, "BTCUSDT", Timeframe.M1, 5)

    assert len(candles) == 5
    assert_closed_utc_candles(candles, timeframe=Timeframe.M1, now=NOW)
    # 10:07:30 now: the 10:07 bar is forming, so the newest closes at 10:07.
    assert candles[-1].ts == datetime(2026, 10, 1, 10, 7, tzinfo=UTC)
    assert candles[0].source_instrument == "BTCUSDT"


async def test_candles_respect_to(provider: BinanceMarketData) -> None:
    to = datetime(2026, 10, 1, 9, 30, 20, tzinfo=UTC)

    candles = await provider.candles(None, "BTCUSDT", Timeframe.M1, 3, to=to)

    assert [candle.ts.minute for candle in candles] == [28, 29, 30]


async def test_large_requests_page_backwards_without_gaps(
    provider: BinanceMarketData, server: FakeBinance
) -> None:
    candles = await provider.candles(None, "BTCUSDT", Timeframe.M1, 2500)

    assert len(candles) == 2500
    steps = {b.ts - a.ts for a, b in zip(candles, candles[1:], strict=False)}
    assert steps == {timedelta(minutes=1)}
    kline_calls = [r for r in server.requests if r.url.path == "/api/v3/klines"]
    assert len(kline_calls) == 3
    assert all(int(r.url.params["limit"]) <= 1000 for r in kline_calls)


async def test_unsupported_timeframe_and_symbol_are_422(provider: BinanceMarketData) -> None:
    with pytest.raises(ServiceError) as timeframe:
        await provider.candles(None, "BTCUSDT", Timeframe.M2, 5)
    with pytest.raises(ServiceError) as symbol:
        await provider.candles(None, "XRPUSDT", Timeframe.M1, 5)
    with pytest.raises(ServiceError) as feed:
        provider.instruments("spot")

    assert (timeframe.value.status_code, timeframe.value.code) == (422, "timeframe_not_supported")
    assert (symbol.value.status_code, symbol.value.code) == (422, "symbol_not_allowed")
    assert feed.value.status_code == 422
    assert provider.resolve_symbols(None, ["btcusdt"]) == frozenset({"BTCUSDT"})


async def test_rate_limit_blocks_further_calls(
    provider: BinanceMarketData, server: FakeBinance
) -> None:
    server.rate_limited = True
    with pytest.raises(ServiceError) as first:
        await provider.candles(None, "BTCUSDT", Timeframe.M1, 5)
    calls = len(server.requests)
    with pytest.raises(ServiceError) as second:
        await provider.candles(None, "BTCUSDT", Timeframe.M1, 5)

    assert first.value.code == "candles_unavailable"
    assert first.value.details["retry_after_seconds"] == 30.0
    assert second.value.details["retry_after_seconds"] > 0
    assert len(server.requests) == calls, "no request may leave while Binance says stop"
    assert provider.readiness()[1]["rate_limited_for_seconds"] > 0


async def test_server_weight_is_adopted(provider: BinanceMarketData, server) -> None:
    server.used_weight = "4000"
    await provider.candles(None, "BTCUSDT", Timeframe.M1, 2)
    assert provider.readiness()[1]["request_weight_used"] >= 4000


async def test_unknown_listing_keeps_the_provider_not_ready(server: FakeBinance) -> None:
    http = httpx.AsyncClient(
        base_url="https://api.binance.test", transport=httpx.MockTransport(server.handler)
    )
    provider = BinanceMarketData(
        _settings(binance_symbols=("BTCUSDT", "NOPEUSDT")),
        http=http,
        ws_connect=FakeStream([]),
        clock=lambda: NOW,
    )
    await provider.start()

    assert await provider.wait_ready(0.05) is False
    assert "NOPEUSDT" in provider.readiness()[1]["last_error"]
    await provider.close()
    await http.aclose()


async def test_stream_reconnects_and_reseeds(server: FakeBinance) -> None:
    http = httpx.AsyncClient(
        base_url="https://api.binance.test", transport=httpx.MockTransport(server.handler)
    )
    stream = FakeStream([], hold_open=False)  # every connection closes at once
    provider = BinanceMarketData(_settings(), http=http, ws_connect=stream, clock=lambda: NOW)
    await provider.start()
    for _ in range(200):
        await asyncio.sleep(0.001)
        if len(stream.urls) >= 3:
            break

    assert len(stream.urls) >= 3
    seeds = [r for r in server.requests if r.url.path == "/api/v3/ticker/bookTicker"]
    assert len(seeds) >= 2, "each reconnect re-seeds the quote cache from REST"
    assert provider.readiness()[1]["reconnects"] >= 2
    await provider.close()
    await http.aclose()


async def test_limiter_waits_for_the_next_window() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = WeightLimiter(10, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire(8)
    await limiter.acquire(5)

    assert slept == [60.0]
    assert limiter.used == 5
