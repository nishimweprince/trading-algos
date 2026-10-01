from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backtesting_service.candle_store import CandleStore
from backtesting_service.config import Settings
from backtesting_service.models import Candle, Timeframe

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")


def test_local_jsonl_round_trip(settings: Settings) -> None:
    store = CandleStore(settings, httpx.AsyncClient())
    candles = [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    path = store.write_local("XAUUSD", Timeframe.M15, candles)
    assert path.is_file()
    loaded = store.load_local("XAUUSD", Timeframe.M15)
    assert len(loaded) == len(candles)
    assert loaded[0].ts == candles[0].ts
    assert loaded[-1].close == candles[-1].close


@pytest.mark.asyncio
async def test_fetch_asks_market_data_service_for_the_configured_market(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        market_data_url="http://md:8021",
        market_data_api_key="market-data-key-0123",
        market_data_market="deriv",
    )
    seen: list[httpx.Request] = []
    bar = Candle(
        ts=datetime(2026, 1, 14, 13, 15, tzinfo=UTC),
        open=1,
        high=2,
        low=0.5,
        close=1.5,
        volume=1,
        provider="mt5",
        source_instrument="Volatility 75 Index",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "symbol": "Volatility 75 Index",
            "timeframe": "M15",
            "candles": [bar.model_dump(mode="json")],
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        candles = await CandleStore(settings, http).fetch(
            "Volatility 75 Index", Timeframe.M15, count=1
        )

    assert candles == [bar]
    assert seen[0].url.host == "md" and seen[0].url.port == 8021
    assert seen[0].url.path == "/v1/deriv/candles"
    assert seen[0].headers["X-API-Key"] == "market-data-key-0123"


@pytest.mark.asyncio
async def test_a_store_without_an_http_client_refuses_to_fetch(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="without an HTTP client"):
        await CandleStore(settings, None).fetch("XAUUSD", Timeframe.M15, count=1)
