from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from ta_core import configure_file_logs, reset_file_logs

from ta_notify import NotificationSettings, Notifier


class Settings(NotificationSettings):
    profile: str | None = None


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "notifications_enabled": True,
        "notification_service_url": "http://notify.local",
        "notification_channels_csv": "TELEGRAM,EMAIL",
    }
    base.update(overrides)
    return Settings(**base)


def notifier(handler, **overrides: Any) -> tuple[Notifier, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    return Notifier(settings(**overrides), client, source="session-hedging"), seen


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(202, json={"accepted": True})


# --- the contract that must not change --------------------------------------


async def test_send_swallows_a_server_error() -> None:
    """A notification failure must never propagate into a trading path."""
    client, _ = notifier(lambda _: httpx.Response(500, text="boom"))
    await client.send("Entry", ["line"])  # must not raise


async def test_send_swallows_a_transport_error() -> None:
    def explode(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client, _ = notifier(explode)
    await client.send("Entry", ["line"])  # must not raise


async def test_failure_is_logged_with_context(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)
    try:
        client, _ = notifier(lambda _: httpx.Response(503))
        await client.send("Entry", ["line"], pair_id="abc")
    finally:
        reset_file_logs()
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event"] == "notification_failed"
    assert event["pair_id"] == "abc"
    assert event["subject"] == "Entry"


# --- payload ----------------------------------------------------------------


async def test_payload_shape() -> None:
    client, seen = notifier(ok)
    await client.send("Entry filled", ["XAUUSD", "0.10 lots"])
    body = json.loads(seen[0].content)
    assert body == {
        "subject": "Entry filled",
        "message": "XAUUSD\n0.10 lots",
        "contentType": "text",
        "channels": ["EMAIL", "TELEGRAM"],
        "source": "session-hedging",
    }


async def test_channels_are_sorted_for_a_stable_payload() -> None:
    client, seen = notifier(ok, notification_channels_csv="WHATSAPP,EMAIL,SMS")
    await client.send("s", ["m"])
    assert json.loads(seen[0].content)["channels"] == ["EMAIL", "SMS", "WHATSAPP"]


async def test_source_is_profile_suffixed() -> None:
    client, seen = notifier(ok, profile="forex")
    await client.send("s", ["m"])
    assert json.loads(seen[0].content)["source"] == "session-hedging.forex"


async def test_posts_to_the_notifications_path() -> None:
    client, seen = notifier(ok, notification_service_url="http://notify.local/")
    await client.send("s", ["m"])
    assert str(seen[0].url) == "http://notify.local/notifications"


# --- auth -------------------------------------------------------------------


async def test_both_auth_headers_are_sent() -> None:
    client, seen = notifier(ok, notification_api_key=SecretStr("secret-value"))
    await client.send("s", ["m"])
    assert seen[0].headers["authorization"] == "Bearer secret-value"
    assert seen[0].headers["x-api-key"] == "secret-value"


async def test_no_auth_headers_without_a_key() -> None:
    client, seen = notifier(ok)
    await client.send("s", ["m"])
    assert "authorization" not in seen[0].headers


# --- enablement -------------------------------------------------------------


async def test_disabled_notifier_makes_no_request() -> None:
    client, seen = notifier(ok, notifications_enabled=False)
    await client.send("s", ["m"])
    assert seen == []


async def test_no_channels_means_disabled() -> None:
    client, seen = notifier(ok, notifications_enabled=False, notification_channels_csv="")
    await client.send("s", ["m"])
    assert seen == []
    assert client.enabled is False


# --- settings ---------------------------------------------------------------


def test_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown NOTIFICATION_CHANNELS"):
        settings(notification_channels_csv="TELEGRAM,PIGEON")


def test_enabled_without_channels_is_rejected() -> None:
    with pytest.raises(ValidationError, match="required when NOTIFICATIONS_ENABLED"):
        settings(notification_channels_csv="")


def test_channels_are_parsed_case_insensitively() -> None:
    assert settings(notification_channels_csv=" telegram , email ").notification_channels == (
        frozenset({"TELEGRAM", "EMAIL"})
    )
