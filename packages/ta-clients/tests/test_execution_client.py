from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from ta_clients import (
    OPERATION_NAMESPACE,
    ExecutionClient,
    ExecutionState,
    client_order_id_for,
    decimal_text,
    operation_id_for,
    safe_reason,
    timestamp_text,
)

OCCURRED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
OP = UUID("11111111-2222-3333-4444-555555555555")


@dataclass
class Settings:
    ctrader_markets_url: str = "http://gateway.local"
    execution_timeout_seconds: float = 5.0
    ctrader_api_key: Any = None
    execution_account: str = "forex-demo"
    execution_source: str = "session_hedging"
    execution_volume_lots: Any = Decimal("0.10")


def build(handler, **overrides: Any) -> tuple[ExecutionClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    return ExecutionClient(Settings(**overrides), client), seen


def json_response(status: int, body: Any):
    return lambda _: httpx.Response(status, json=body)


# --- the UNKNOWN-not-REJECTED contract --------------------------------------


async def test_transport_failure_is_unknown_not_rejected() -> None:
    """The order may have reached the broker. Reconcile, never resubmit."""

    def explode(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    client, _ = build(explode)
    result = await client.submit({"any": "payload"})
    assert result.state is ExecutionState.UNKNOWN
    assert result.state is not ExecutionState.REJECTED


async def test_timeout_is_unknown() -> None:
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client, _ = build(slow)
    assert (await client.submit({})).state is ExecutionState.UNKNOWN


async def test_500_is_unknown() -> None:
    client, _ = build(json_response(500, {"error": {"code": "internal"}}))
    assert (await client.submit({})).state is ExecutionState.UNKNOWN


async def test_malformed_body_is_unknown() -> None:
    client, _ = build(lambda _: httpx.Response(200, text="not json"))
    assert (await client.submit({})).state is ExecutionState.UNKNOWN


# --- decisions, which must NOT be retried blindly ---------------------------


async def test_409_conflict_is_rejected() -> None:
    """A reused operation_id with a changed payload is a decision, not an outage."""
    client, _ = build(json_response(409, {"error": {"code": "operation_conflict"}}))
    result = await client.submit({})
    assert result.state is ExecutionState.REJECTED
    assert result.reason == "operation_conflict"


async def test_422_validation_is_rejected() -> None:
    client, _ = build(json_response(422, {"error": {"code": "validation_error"}}))
    assert (await client.submit({})).state is ExecutionState.REJECTED


async def test_rejected_operation_state_is_rejected() -> None:
    client, _ = build(json_response(200, {"state": "rejected", "targets": []}))
    assert (await client.submit({})).state is ExecutionState.REJECTED


async def test_partial_failure_is_rejected() -> None:
    client, _ = build(json_response(200, {"state": "partial_failure", "targets": []}))
    assert (await client.submit({})).state is ExecutionState.REJECTED


# --- success and pending ----------------------------------------------------


async def test_202_is_pending() -> None:
    client, _ = build(json_response(202, {"state": "pending", "targets": []}))
    assert (await client.submit({})).state is ExecutionState.PENDING


async def test_pending_state_is_pending() -> None:
    client, _ = build(json_response(200, {"state": "pending", "targets": []}))
    assert (await client.submit({})).state is ExecutionState.PENDING


async def test_succeeded_is_succeeded() -> None:
    client, _ = build(json_response(200, {"state": "succeeded", "targets": []}))
    assert (await client.submit({})).state is ExecutionState.SUCCEEDED


async def test_missing_operation_is_not_found() -> None:
    client, _ = build(json_response(404, {"error": {"code": "not_found"}}))
    assert (await client.get_operation(OP)).state is ExecutionState.NOT_FOUND


# --- result accessors -------------------------------------------------------


async def test_order_ids_are_keyed_by_account() -> None:
    body = {"state": "succeeded", "targets": [{"account": "forex-demo", "order_id": 77}]}
    client, _ = build(json_response(200, body))
    assert (await client.submit({})).order_ids == {"forex-demo": 77}


async def test_fill_price_reads_the_matching_target() -> None:
    body = {
        "state": "succeeded",
        "targets": [
            {"account": "other", "execution_price": "1.0"},
            {"account": "forex-demo", "execution_price": "2001.25"},
        ],
    }
    client, _ = build(json_response(200, body))
    assert (await client.submit({})).fill_price("forex-demo") == 2001.25


# --- idempotency primitives -------------------------------------------------


def test_operation_namespace_is_pinned() -> None:
    """Changing this makes every in-flight operation look new to the gateway."""
    assert str(OPERATION_NAMESPACE) == "6f2a1d3c-8b74-4e59-9a10-2f5c7d8e4b16"


def test_operation_id_is_deterministic() -> None:
    """A restart mid-submit must recompute the same id, not open a second position."""
    first = operation_id_for(symbol="XAUUSD", pair_id="p1", side="long")
    second = operation_id_for(symbol="XAUUSD", pair_id="p1", side="long")
    assert first == second


def test_operation_id_varies_by_leg_and_attempt() -> None:
    base = dict(symbol="XAUUSD", pair_id="p1")
    assert operation_id_for(**base, side="long") != operation_id_for(**base, side="short")
    assert operation_id_for(**base, side="long") != operation_id_for(**base, side="long", attempt=1)


def test_client_order_id_is_bounded_and_stable() -> None:
    value = client_order_id_for(OP, "forex-demo")
    assert value == client_order_id_for(OP, "forex-demo")
    assert len(value) <= 50


def test_decimal_text_avoids_exponent_notation() -> None:
    """The gateway hashes the payload, so 1E-2 and 0.01 are different requests."""
    assert decimal_text(0.01) == "0.01"
    assert "E" not in decimal_text(0.00001)


def test_decimal_text_trims_trailing_zeros() -> None:
    assert decimal_text(Decimal("0.10")) == "0.1"
    assert decimal_text(2) == "2"


def test_timestamp_text_is_zulu() -> None:
    assert timestamp_text(OCCURRED) == "2026-01-02T03:04:05Z"


def test_timestamp_text_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        timestamp_text(datetime(2026, 1, 2))


def test_safe_reason_falls_back_rather_than_printing_none() -> None:
    assert safe_reason(None, fallback="HTTP 500") == "HTTP 500"
    assert safe_reason("  ", fallback="HTTP 500") == "HTTP 500"


def test_safe_reason_is_single_line_and_bounded() -> None:
    value = safe_reason("a\nb" + "x" * 500, fallback="f")
    assert "\n" not in value
    assert len(value) <= 240


# --- payload building -------------------------------------------------------


async def test_stop_entry_payload_uses_distances_not_levels() -> None:
    """Protection anchors to the actual fill, which only the broker knows."""
    client, _ = build(json_response(200, {"state": "succeeded"}))
    payload = client.build_stop_entry(
        operation_id=OP,
        occurred_at=OCCURRED,
        symbol="xauusd",
        direction="buy",
        entry_price=2000.0,
        stop_distance=1.5,
        target_distance=4.5,
        expires_at=None,
    )
    assert payload["instrument"] == "XAUUSD"
    assert payload["stop_loss_distance"] == "1.5"
    assert "stop_loss" not in payload
    assert payload["targets"] == [{"account": "forex-demo", "volume_lots": "0.1"}]


async def test_expiry_switches_time_in_force_to_gtd() -> None:
    """GTD lets the broker expire an untriggered bracket if a poll is missed."""
    client, _ = build(json_response(200, {}))
    payload = client.build_stop_entry(
        operation_id=OP,
        occurred_at=OCCURRED,
        symbol="XAUUSD",
        direction="sell",
        entry_price=2000.0,
        stop_distance=1.0,
        target_distance=3.0,
        expires_at=OCCURRED,
    )
    assert payload["time_in_force"] == "gtd"
    assert payload["expires_at"] == "2026-01-02T03:04:05Z"


async def test_api_key_header_is_sent() -> None:
    class Key:
        @staticmethod
        def get_secret_value() -> str:
            return "gateway-secret"

    client, seen = build(json_response(200, {}), ctrader_api_key=Key())
    await client.submit({"a": 1})
    assert seen[0].headers["x-api-key"] == "gateway-secret"


async def test_submit_posts_to_the_orders_path() -> None:
    client, seen = build(json_response(200, {}))
    await client.submit({"a": 1})
    assert str(seen[0].url) == "http://gateway.local/v1/orders"
    assert json.loads(seen[0].content) == {"a": 1}


async def test_cancel_targets_the_configured_account() -> None:
    client, seen = build(json_response(200, {}))
    await client.cancel_order(operation_id=OP, occurred_at=OCCURRED, order_id=42)
    body = json.loads(seen[0].content)
    assert body["targets"] == [{"account": "forex-demo", "order_id": 42}]
    assert body["source"] == "session_hedging"


async def test_amend_protection_uses_absolute_levels() -> None:
    client, seen = build(json_response(200, {}))
    await client.amend_protection(
        operation_id=OP, occurred_at=OCCURRED, position_id=9, stop_loss=1995.0
    )
    target = json.loads(seen[0].content)["targets"][0]
    assert target == {"account": "forex-demo", "position_id": 9, "stop_loss": "1995"}


# --- readiness --------------------------------------------------------------


async def test_trading_ready_reports_ready() -> None:
    client, _ = build(json_response(200, {"status": "ready"}))
    assert await client.trading_ready() == (True, "ready")


async def test_trading_ready_reports_the_reason_when_not_ready() -> None:
    client, _ = build(json_response(503, {"reason": "broker_disconnected"}))
    ready, reason = await client.trading_ready()
    assert ready is False
    assert reason == "broker_disconnected"


async def test_trading_ready_survives_a_transport_error() -> None:
    def explode(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, _ = build(explode)
    ready, _ = await client.trading_ready()
    assert ready is False


async def test_list_orders_unwraps_a_bare_array() -> None:
    client, _ = build(json_response(200, [{"order_id": 1}, "junk"]))
    assert await client.list_orders() == [{"order_id": 1}]
