from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from ipda.config import Settings
from ipda.data_client import MarketDataClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        MARKET_DATA_URL="http://md:8023",
        MARKET_DATA_API_KEY="market-data-key-0123",
        MARKET_DATA_MARKET="deriv",
        QUOTE="Volatility 75 Index",
        MT5_SYMBOL="Volatility 75 Index",
        VOLUME="0.10",
        MT5_SIGNAL_API_KEY="test-api-key-with-16-characters",
        LOGS_DIR=str(tmp_path / "logs"),
        DATA_LOOKBACK=2,
    )


def _client(tmp_path: Path, handler) -> MarketDataClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MarketDataClient(_settings(tmp_path), http)


async def test_minute_candles_are_restamped_at_interval_start(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        bar = {
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 3.0,
            "provider": "mt5",
            "source_instrument": "Volatility 75 Index",
        }
        return httpx.Response(
            200,
            json={
                "symbol": "Volatility 75 Index",
                "timeframe": "M1",
                "candles": [
                    {**bar, "ts": "2026-03-02T10:00:00Z"},
                    {**bar, "ts": "2026-03-02T10:01:00Z"},
                ],
            },
        )

    candles = await _client(tmp_path, handler).fetch_minute_candles("Volatility 75 Index")

    assert [c.start for c in candles] == [
        datetime(2026, 3, 2, 9, 59, tzinfo=UTC),
        datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
    ]
    assert all(c.closed for c in candles)
    assert seen[0].url.path == "/v1/deriv/candles"
    assert seen[0].url.params["timeframe"] == "M1"
    assert seen[0].headers["X-API-Key"] == "market-data-key-0123"


async def test_tick_needs_real_bid_and_ask(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbol": "Volatility 75 Index",
                "source_instrument": "Volatility 75 Index",
                "provider": "mt5",
                "ts": "2026-03-02T10:00:00Z",
                "price": 100.0,
            },
        )

    with pytest.raises(ValueError, match="no bid/ask"):
        await _client(tmp_path, handler).fetch_tick("Volatility 75 Index")


async def test_tick_carries_bid_and_ask(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/deriv/tick"
        return httpx.Response(
            200,
            json={
                "symbol": "Volatility 75 Index",
                "source_instrument": "Volatility 75 Index",
                "provider": "mt5",
                "ts": "2026-03-02T10:00:00Z",
                "bid": 100.1,
                "ask": 100.3,
            },
        )

    tick = await _client(tmp_path, handler).fetch_tick("Volatility 75 Index")

    assert (tick.symbol, tick.bid, tick.ask) == ("Volatility 75 Index", 100.1, 100.3)
