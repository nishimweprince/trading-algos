"""The delivery result, the idempotency key, and the sync transport.

These three exist for lookup-trader, whose own client had them and ta-notify's
did not -- the reason it could not simply adopt the shared one. They are useful
everywhere: a caller that persists whether an alert went out could not previously
find out, and a retry after an ambiguous failure could produce a second alert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from ta_notify import NotificationResult, Notifier, SyncNotifier


@dataclass
class Settings:
    notifications_enabled: bool = True
    notification_service_url: str = "http://127.0.0.1:3010"
    notification_api_key: Any = None
    notification_timeout_seconds: float = 5.0
    profile: str | None = None
    channels: frozenset[str] = frozenset({"TELEGRAM"})

    @property
    def notification_channels(self) -> frozenset[str]:
        return self.channels


def _async(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sync(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# Factories, not instances. An httpx.Response is stateful -- its body is read
# once -- so sharing one across tests passes in isolation and fails in a full run.
RESPONSES: dict[str, Any] = {
    "sent": lambda: httpx.Response(201, json={"requestId": "req-1"}),
    "remote_skipped": lambda: httpx.Response(200, json={"status": "skipped", "requestId": "req-2"}),
    "accepted_unknown_shape": lambda: httpx.Response(202, json={}),
    "server_error": lambda: httpx.Response(500),
}


@pytest.mark.parametrize(
    ("key", "status", "request_id"),
    [
        ("sent", "sent", "req-1"),
        ("remote_skipped", "remote_skipped", "req-2"),
        ("accepted_unknown_shape", "sent", None),
        ("server_error", "failed", None),
    ],
)
async def test_response_maps_onto_a_result(key: str, status: str, request_id: str | None) -> None:
    async with _async(lambda request: RESPONSES[key]()) as http:
        result = await Notifier(Settings(), http, source="svc").send("s", ["l"])

    assert result.status == status
    assert result.request_id == request_id


async def test_remote_skipped_counts_as_delivered() -> None:
    """The service accepted it and chose not to deliver. That is not our failure."""
    async with _async(lambda request: RESPONSES["remote_skipped"]()) as http:
        result = await Notifier(Settings(), http, source="svc").send("s", ["l"])

    assert result.delivered is True


@pytest.mark.parametrize("status", ["failed", "disabled"])
def test_undelivered_statuses_are_not_delivered(status: str) -> None:
    assert NotificationResult(status).delivered is False


async def test_transport_failure_is_a_failed_result_and_never_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _async(boom) as http:
        result = await Notifier(Settings(), http, source="svc").send("s", ["l"])

    assert result.status == "failed"
    assert "refused" in (result.error or "")


async def test_disabled_reports_disabled_rather_than_failed() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a disabled notifier must not call the service")

    async with _async(unreachable) as http:
        result = await Notifier(Settings(notifications_enabled=False), http, source="svc").send(
            "s", ["l"]
        )

    assert result.status == "disabled"
    assert result.delivered is False


async def test_idempotency_key_is_sent_when_given() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return RESPONSES["sent"]()

    async with _async(handler) as http:
        await Notifier(Settings(), http, source="svc").send("s", ["l"], idempotency_key="key-1")

    assert seen[0]["idempotencyKey"] == "key-1"


async def test_idempotency_key_is_absent_when_not_given() -> None:
    """Omitted rather than null: the field is optional on the wire."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return RESPONSES["sent"]()

    async with _async(handler) as http:
        await Notifier(Settings(), http, source="svc").send("s", ["l"])

    assert "idempotencyKey" not in seen[0]


def test_sync_notifier_sends_the_same_payload_as_the_async_one() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return RESPONSES["sent"]()

    with _sync(handler) as http:
        result = SyncNotifier(Settings(), http, source="svc").send(
            "subject", ["a", "b"], idempotency_key="k"
        )

    assert result.status == "sent"
    assert seen[0] == {
        "subject": "subject",
        "message": "a\nb",
        "contentType": "text",
        "channels": ["TELEGRAM"],
        "source": "svc",
        "idempotencyKey": "k",
    }


def test_sync_notifier_never_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _sync(boom) as http:
        result = SyncNotifier(Settings(), http, source="svc").send("s", ["l"])

    assert result.status == "failed"


def test_sync_notifier_suffixes_the_profile_like_the_async_one() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return RESPONSES["sent"]()

    with _sync(handler) as http:
        SyncNotifier(Settings(profile="shadow"), http, source="svc").send("s", ["l"])

    assert seen[0]["source"] == "svc.shadow"
