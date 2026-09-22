from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from backtesting_service.candle_store import Mt5CandleError, Mt5CandleStore, create_candle_store
from backtesting_service.config import Settings
from backtesting_service.models import Timeframe


def config(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        market_data_provider="mt5",
        mt5_signal_api_key="test-key",
        mt5_market_data_symbol="XAUUSDb",
        mt5_market_data_server_utc_offset_seconds=10800,
        **overrides,
    )


def response() -> dict[str, Any]:
    now = datetime.now(UTC)
    current_open = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    return {
        "symbol": "XAUUSDb",
        "timeframe": "M15",
        "candles": [
            {
                "time": int((current_open - timedelta(minutes=15 * i)).timestamp()) + 10800,
                "open": 4200,
                "high": 4202,
                "low": 4199,
                "close": 4201,
                "volume": 10,
            }
            for i in (0, 1, 2)
        ],
    }


@pytest.mark.asyncio
async def test_mt5_uses_broker_symbol_auth_and_only_closed_utc_interval_end_bars() -> None:
    payload = response()
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.port == 8000
        assert request.url.params["quote"] == "XAUUSDb"
        assert "symbol" not in request.url.params
        assert request.headers["X-API-Key"] == "test-key"
        assert request.url.params["count"] == "3"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        store = create_candle_store(config(), http)
        assert isinstance(store, Mt5CandleStore)
        bars = await store.fetch_ctrader("XAUUSD", Timeframe.M15, count=2)
    assert len(calls) == 1
    assert len(bars) == 2
    assert bars[0].ts < bars[1].ts <= datetime.now(UTC)
    assert bars[-1].ts.timestamp() == payload["candles"][0]["time"] - 10800
    assert all(bar.provider == "mt5" and bar.source_instrument == "XAUUSDb" for bar in bars)


@pytest.mark.asyncio
async def test_mt5_history_boundary_filters_window_and_rejects_older_unavailable_history() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))
    ) as http:
        store = Mt5CandleStore(config(), http)
        bars = await store.fetch_ctrader("XAUUSD", Timeframe.M15, count=2)
        older = await store.fetch_ctrader("XAUUSD", Timeframe.M15, count=2, to=bars[0].ts)
        assert older == bars[:1]
        with pytest.raises(Mt5CandleError, match="older"):
            await store.fetch_ctrader(
                "XAUUSD", Timeframe.M15, count=2, to=bars[0].ts - timedelta(days=100)
            )
        with pytest.raises(Mt5CandleError, match="5000"):
            await store.fetch_ctrader("XAUUSD", Timeframe.M15, count=6000)


@pytest.mark.asyncio
async def test_mt5_readiness_reports_gateway_symbol_failure_without_fallback() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"error": {"code": "symbol_not_allowed"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        ready, reason = await Mt5CandleStore(config(), http).gateway_ready()
    assert not ready
    assert "422" in reason
    assert len(calls) == 1


def test_candle_provider_default_preserves_ctrader_and_mt5_requires_auth() -> None:
    from pydantic import ValidationError
    from ta_clients import CandleStore

    assert Settings(_env_file=None).market_data_provider == "ctrader"
    assert type(create_candle_store(Settings(_env_file=None), None)) is CandleStore
    with pytest.raises(ValidationError, match="MT5_SIGNAL_API_KEY"):
        Settings(_env_file=None, market_data_provider="mt5")
