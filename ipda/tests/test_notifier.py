from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ipda.config import Settings
from ipda.notifier import Notifier


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "DATA_API_URL": "http://127.0.0.1:8000/v1/market-data/candles",
        "QUOTE": "EURUSD",
        "MT5_SYMBOL": "EURUSD",
        "VOLUME": "0.10",
        "MT5_SIGNAL_API_KEY": "test-api-key-with-16-characters",
        "NOTIFICATIONS_ENABLED": True,
        "NOTIFICATION_SERVICE_URL": "http://127.0.0.1:3010",
        "NOTIFICATION_API_KEY": "notify-key",
    }
    base.update(overrides)
    return Settings(**base)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_send_posts_the_notification_service_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "1"})

    async with _client(handler) as http:
        notifier = Notifier(_settings(), http)
        await notifier.send("EURUSD BUY — skipped", ["symbol: EURUSD", "direction: buy"])

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "http://127.0.0.1:3010/notifications"
    assert request.headers["Authorization"] == "Bearer notify-key"
    assert request.headers["X-API-Key"] == "notify-key"

    body = json.loads(request.content)
    assert body == {
        "subject": "EURUSD BUY — skipped",
        "message": "symbol: EURUSD\ndirection: buy",
        "contentType": "text",
        "channels": ["TELEGRAM"],
        "source": "ipda",
    }


async def test_source_carries_the_profile() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201)

    settings = _settings().model_copy(update={"profile": "deriv"})
    async with _client(handler) as http:
        await Notifier(settings, http).send("subject", ["line"])

    assert seen[0]["source"] == "ipda.deriv"


async def test_disabled_notifier_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled notifier must not call the service")

    async with _client(handler) as http:
        notifier = Notifier(_settings(NOTIFICATIONS_ENABLED=False), http)
        assert notifier.enabled is False
        await notifier.send("subject", ["line"])


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda request: httpx.Response(500), id="server_error"),
        pytest.param(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
            id="transport_error",
        ),
    ],
)
async def test_failures_never_propagate(handler: Any) -> None:
    """A notification outage must not be able to stop the trading loop."""
    async with _client(handler) as http:
        await Notifier(_settings(), http).send("subject", ["line"])


async def test_multiple_channels_are_sorted() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201)

    async with _client(handler) as http:
        notifier = Notifier(_settings(NOTIFICATION_CHANNELS="telegram, EMAIL"), http)
        await notifier.send("subject", ["line"])

    assert seen[0]["channels"] == ["EMAIL", "TELEGRAM"]
