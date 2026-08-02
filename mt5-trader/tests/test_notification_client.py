from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mt5_signal_service.config import Settings
from mt5_signal_service.notification_client import NotificationClient


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "terminal_path": Path("C:/MT5/terminal64.exe"),
        "login": 12345678,
        "password": "secret-password",
        "server": "Broker-Demo",
        "api_key": "test-api-key-with-16-characters",
        "allowed_symbols_csv": "EURUSD",
        "maximum_volume": "1.00",
        "magic_number": 234000,
        "database_path": Path("C:/data/signals.sqlite3"),
        "notifications_enabled": True,
        "notification_service_url": "http://127.0.0.1:3010",
        "notification_api_key": "notification-api-key-secret",
        "notification_channels_csv": "TELEGRAM,EMAIL",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_notification_channels_parsed() -> None:
    settings = _settings(notification_channels_csv="telegram, email ,SMS")
    assert settings.notification_channels == frozenset({"TELEGRAM", "EMAIL", "SMS"})


@pytest.mark.asyncio
async def test_notify_skipped_when_disabled() -> None:
    client = NotificationClient(_settings(notifications_enabled=False))
    with patch(
        "mt5_signal_service.notification_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
    ) as post:
        await client.notify_signal_outcome({"signal_id": "abc", "state": "filled"})
    post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_posts_payload_with_auth() -> None:
    client = NotificationClient(_settings(profile="forex"))
    summary = {
        "signal_id": "11111111-1111-1111-1111-111111111111",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": "0.10",
        "state": "filled",
        "signal_source": "lux_algo",
        "profile": "forex",
        "outcome": "filled",
        "error": None,
    }

    captured: dict[str, object] = {}
    mock_http = AsyncMock()

    async def mock_post(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(
            201, json={"requestId": "r1", "deliveryIds": [], "deliveriesAttempted": 1}
        )

    mock_http.post = mock_post
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "mt5_signal_service.notification_client.httpx.AsyncClient",
        return_value=mock_http,
    ):
        await client.notify_signal_outcome(summary)

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer notification-api-key-secret"
    body = kwargs["json"]
    assert isinstance(body, dict)
    assert body["source"] == "mt5-trader.forex"
    assert body["channels"] == ["EMAIL", "TELEGRAM"]
    assert "EURUSD buy" in body["subject"]
    assert "11111111-1111-1111-1111-111111111111" in body["message"]


@pytest.mark.asyncio
async def test_notify_includes_stop_adjustment_note() -> None:
    client = NotificationClient(_settings(profile="deriv"))
    summary = {
        "signal_id": "11111111-1111-1111-1111-111111111111",
        "symbol": "Volatility 75 Index",
        "direction": "sell",
        "volume": "0.01",
        "state": "filled",
        "signal_source": "lux_algo",
        "profile": "deriv",
        "outcome": "filled",
        "error": None,
        "stop_adjustments": {
            "stop_loss": {
                "requested_distance": "108.77",
                "applied_distance": "125.87",
                "minimum_distance": "123.71",
            }
        },
    }

    captured: dict[str, object] = {}
    mock_http = AsyncMock()

    async def mock_post(url: str, **kwargs: object) -> httpx.Response:
        captured["kwargs"] = kwargs
        return httpx.Response(
            201, json={"requestId": "r1", "deliveryIds": [], "deliveriesAttempted": 1}
        )

    mock_http.post = mock_post
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "mt5_signal_service.notification_client.httpx.AsyncClient",
        return_value=mock_http,
    ):
        await client.notify_signal_outcome(summary)

    body = captured["kwargs"]["json"]
    assert isinstance(body, dict)
    message = body["message"]
    assert "broker stop levels adjusted during execution" in message
    assert "SL widened: requested 108.77, applied 125.87" in message


@pytest.mark.asyncio
async def test_notify_swallows_transport_errors() -> None:
    client = NotificationClient(_settings())
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=httpx.HTTPError("down"))
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "mt5_signal_service.notification_client.httpx.AsyncClient",
        return_value=mock_http,
    ):
        await client.notify_signal_outcome(
            {
                "signal_id": "abc",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": "0.10",
                "state": "rejected",
                "signal_source": "lux_algo",
                "profile": None,
                "outcome": None,
                "error": {"code": "preflight_rejected"},
            }
        )
