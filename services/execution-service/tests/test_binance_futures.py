"""execution-service with ADAPTERS=binance_futures, end to end over HTTP, on a fake venue."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from ta_plugin_binance_futures.execution import BinanceFuturesExecution
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.testing import FakeBinanceTrading, FakeUserStream

from execution_service.api import create_app
from tests.conftest import build_settings

ACCOUNT = "binance_testnet"
KEY = {"X-API-Key": "test-api-key-at-least-16"}


def binance_settings(tmp_path: Path, **overrides: Any):
    values: dict[str, Any] = {
        "ADAPTERS": "binance_futures",
        "BINANCE_FUTURES_SYMBOLS": "BTCUSDT,ETHUSDT",
        "BINANCE_FUTURES_ENV": "testnet",
        "BINANCE_FUTURES_TRADING_API_KEY": "t" * 64,
        "BINANCE_FUTURES_TRADING_API_SECRET": "u" * 64,
        "BINANCE_FUTURES_TRADING_REST_URL": "https://demo-fapi.binance.test",
        "BINANCE_FUTURES_TRADING_WS_URL": "wss://demo-fstream.binance.test",
        "MAX_VOLUME_LOTS": "0.01",
        "ALLOWED_ORDER_SOURCES": "ofi_scalper",
        "TRADING_ENABLED": True,
        "EXECUTION_DATABASE_PATH": tmp_path / "executions.sqlite3",
        "STARTUP_READY_TIMEOUT_SECONDS": 5,
        "RECONCILE_INTERVAL_SECONDS": 0,
    }
    values.update(overrides)
    return build_settings(tmp_path, **values)


def venue_for(settings: Any, fake: FakeBinanceTrading) -> BinanceFuturesExecution:
    http = httpx.AsyncClient(
        base_url=settings.binance_futures_order_rest_url,
        transport=httpx.MockTransport(fake.handler),
    )
    rest = FapiRest(
        settings,
        http=http,
        api_key=settings.binance_futures_trading_api_key,
        api_secret=settings.binance_futures_trading_api_secret,
    )
    return BinanceFuturesExecution(settings, rest=rest, ws_connect=FakeUserStream(fake))


def order(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operation_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "source": "ofi_scalper",
        "instrument": "BTCUSDT",
        "execution_type": "limit",
        "direction": "buy",
        "targets": [{"account": ACCOUNT, "volume_lots": "0.002"}],
        "entry_price": "59999.9",
        "post_only": True,
    }
    body.update(fields)
    if body["execution_type"] == "market":
        body.pop("entry_price", None)
        body.pop("post_only", None)
    return body


def wait_for(client: TestClient, operation_id: str, state: str, seconds: float = 3.0) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        body = client.get(f"/v1/operations/{operation_id}", headers=KEY).json()
        if body["targets"][0]["state"] == state or time.monotonic() > deadline:
            return body
        time.sleep(0.01)


def test_binance_futures_needs_a_trading_key_cap_and_sources(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        binance_settings(
            tmp_path,
            BINANCE_FUTURES_TRADING_API_KEY=None,
            MAX_VOLUME_LOTS=None,
            ALLOWED_ORDER_SOURCES="",
        )
    message = str(caught.value)
    for name in ("BINANCE_FUTURES_TRADING_API_KEY", "MAX_VOLUME_LOTS", "ALLOWED_ORDER_SOURCES"):
        assert name in message


def test_orders_fills_and_kill_controls_over_http(tmp_path: Path) -> None:
    settings = binance_settings(tmp_path)
    fake = FakeBinanceTrading()
    app = create_app(settings, extra_providers=[venue_for(settings, fake)])
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/health/trading-ready").status_code == 200
        accounts = client.get("/v1/accounts", headers=KEY).json()["accounts"]
        assert accounts[0]["alias"] == ACCOUNT and accounts[0]["environment"] == "testnet"

        # Post-only limit: rests, then the user stream settles the fill.
        entry = order()
        placed = client.post("/v1/orders", json=entry, headers=KEY)
        assert placed.status_code == 201, placed.text
        target = placed.json()["targets"][0]
        assert target["state"] == "placed" and target["order_id"]
        posts = sum(
            1 for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order"
        )

        replay = client.post("/v1/orders", json=entry, headers=KEY)  # same operation: no resend
        assert replay.json()["targets"][0]["order_id"] == target["order_id"]
        assert posts == sum(
            1 for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order"
        )

        fake.fill(target["order_id"])
        filled = wait_for(client, entry["operation_id"], "filled")
        assert filled["targets"][0]["state"] == "filled"
        assert filled["targets"][0]["executed_volume_lots"] == "0.002"

        # Post-only that would take: rejected by Binance, nothing placed.
        taker = client.post("/v1/orders", json=order(entry_price="60000.1"), headers=KEY).json()
        assert taker["state"] == "rejected"
        assert taker["targets"][0]["error_code"] == "post_only_would_take"

        # Over MAX_VOLUME_LOTS: refused before the ledger.
        big = order(targets=[{"account": ACCOUNT, "volume_lots": "0.02"}])
        assert client.post("/v1/orders", json=big, headers=KEY).status_code == 422

        # Kill path: dead-man, cancel-all, flatten.
        client.post("/v1/orders", json=order(entry_price="59990.0"), headers=KEY)
        armed = client.post(
            f"/v1/accounts/{ACCOUNT}/dead-man",
            json={"instruments": ["BTCUSDT"], "countdown_ms": 15000},
            headers=KEY,
        )
        assert armed.status_code == 200 and armed.json()["ok"]
        assert fake.countdowns == {"BTCUSDT": 15000}
        cancelled = client.post(f"/v1/accounts/{ACCOUNT}/cancel-all", json={}, headers=KEY)
        assert cancelled.json()["ok"]
        assert not any(o["status"] == "NEW" for o in fake.orders.values())
        flat = client.post(f"/v1/accounts/{ACCOUNT}/flatten", json={}, headers=KEY)
        assert flat.json()["ok"] and fake.positions["BTCUSDT"] == 0

        missing = client.post("/v1/accounts/nope/cancel-all", json={}, headers=KEY)
        assert missing.status_code == 404
        bad = client.post(
            f"/v1/accounts/{ACCOUNT}/cancel-all", json={"instrument": "DOGEUSDT"}, headers=KEY
        )
        assert bad.status_code == 422


def test_kill_controls_work_with_trading_disabled(tmp_path: Path) -> None:
    settings = binance_settings(tmp_path, TRADING_ENABLED=False)
    fake = FakeBinanceTrading()
    app = create_app(settings, extra_providers=[venue_for(settings, fake)])
    with TestClient(app) as client:
        assert client.get("/health/trading-ready").status_code == 503
        refused = client.post("/v1/orders", json=order(), headers=KEY)
        assert refused.status_code == 503
        # Removing risk is always allowed.
        assert client.post(f"/v1/accounts/{ACCOUNT}/cancel-all", json={}, headers=KEY).json()["ok"]
        assert client.post(f"/v1/accounts/{ACCOUNT}/flatten", json={}, headers=KEY).json()["ok"]


def test_mainnet_is_not_trading_ready_without_live_flag(tmp_path: Path) -> None:
    settings = binance_settings(tmp_path, BINANCE_FUTURES_ENV="mainnet")
    app = create_app(settings, extra_providers=[venue_for(settings, FakeBinanceTrading())])
    with TestClient(app) as client:
        response = client.get("/health/trading-ready")
        assert response.status_code == 503
        assert "LIVE_TRADING_ENABLED" in response.json()["details"]["reason"]
