"""The real Binance provider plugin behind the real app, with only the exchange faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from ta_contracts import MarketKind
from ta_plugin_binance.market_data import BinanceMarketData
from ta_plugin_binance.testing import FakeBinance, FakeStream, book_ticker_frames

from market_data_service.api import create_app
from market_data_service.config import MarketBinding
from tests.conftest import AUTH, build_settings


def test_crypto_market_serves_binance_candles_quotes_and_instruments(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    settings = build_settings(
        tmp_path,
        BINANCE_SYMBOLS="BTCUSDT,ETHUSDT",
        BINANCE_REST_URL="https://api.binance.test",
        markets={MarketKind.CRYPTO: MarketBinding(provider="binance")},
    )
    exchange = FakeBinance(now)
    http = httpx.AsyncClient(
        base_url=settings.binance_rest_url, transport=httpx.MockTransport(exchange.handler)
    )
    stream = FakeStream(book_ticker_frames())
    provider = BinanceMarketData(settings, http=http, ws_connect=stream)
    app = create_app(settings, providers={"binance": provider})

    with TestClient(app) as client:
        candles = client.get(
            "/v1/crypto/candles",
            params={"symbol": "BTCUSDT", "timeframe": "M1", "count": 3},
            headers=AUTH,
        )
        quote = client.get("/v1/crypto/tick", params={"symbol": "ETHUSDT"}, headers=AUTH)
        symbols = client.get("/v1/crypto/symbols", headers=AUTH)
        capabilities = client.get("/v1/crypto/capabilities", headers=AUTH)
        wrong_provider = client.get(
            "/v1/crypto/tick", params={"symbol": "ETHUSDT", "provider": "mt5"}, headers=AUTH
        )
        stream.release.set()

    assert candles.status_code == 200, candles.text
    stamps = [datetime.fromisoformat(c["ts"]) for c in candles.json()["candles"]]
    assert len(stamps) == 3
    assert stamps[-1] <= now < stamps[-1] + timedelta(minutes=1)
    assert quote.status_code == 200, quote.text
    assert quote.json()["provider"] == "binance"
    assert quote.json()["bid"] > 0
    assert [row["symbol"] for row in symbols.json()["instruments"]] == ["BTCUSDT", "ETHUSDT"]
    assert capabilities.json()["provider"] == "binance"
    assert "M2" not in capabilities.json()["timeframes"]
    assert wrong_provider.status_code == 422
