"""cTrader execution client: payload shape, response classification, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from config import Settings
from execution import (
    ExecutionClient,
    ExecutionState,
    client_order_id_for,
    decimal_text,
    operation_id_for,
    timestamp_text,
)

OCCURRED = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)


def _settings(**over: object) -> Settings:
    return Settings(
        market_execution_mode="live",
        execution_account="forex_demo",
        execution_volume_lots=0.01,
        ctrader_api_key="0123456789abcdef0123",
        **over,  # type: ignore[arg-type]
    )


def _client(handler) -> ExecutionClient:
    transport = httpx.MockTransport(handler)
    return ExecutionClient(_settings(), httpx.AsyncClient(transport=transport))


class TestDecimalRendering:
    """The gateway hashes the submitted payload, so text form is part of identity."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.01, "0.01"),
            (1e-2, "0.01"),
            (2686.8900000000003, "2686.89"),
            (100.0, "100"),
            (3.5, "3.5"),
        ],
    )
    def test_never_uses_exponent_notation(self, value: float, expected: str) -> None:
        assert decimal_text(value) == expected

    def test_timestamp_requires_a_timezone(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            timestamp_text(datetime(2026, 1, 5, 14, 0))

    def test_timestamp_is_zulu(self) -> None:
        assert timestamp_text(OCCURRED) == "2026-01-05T14:00:00Z"


class TestIdempotency:
    def test_operation_id_is_stable_for_the_same_structure(self) -> None:
        first = operation_id_for(symbol="XAUUSD", pair_id="new_york:2026-01-05", side="long")
        again = operation_id_for(symbol="XAUUSD", pair_id="new_york:2026-01-05", side="long")
        assert first == again, "a restart must recompute the same id, not open a second position"

    def test_each_leg_and_symbol_gets_its_own_id(self) -> None:
        long_id = operation_id_for(symbol="XAUUSD", pair_id="ny:1", side="long")
        short_id = operation_id_for(symbol="XAUUSD", pair_id="ny:1", side="short")
        other = operation_id_for(symbol="EURUSD", pair_id="ny:1", side="long")
        assert len({long_id, short_id, other}) == 3

    def test_client_order_id_matches_the_gateway_derivation(self) -> None:
        operation_id = UUID("9f0d1c8e-1b2a-4b6f-8f1e-2c9a7d3e5b10")
        derived = client_order_id_for(operation_id, "forex_demo")
        assert derived.startswith(operation_id.hex)
        assert len(derived) <= 50


class TestStopEntryPayload:
    @staticmethod
    def _payload(**over: object) -> dict:
        client = _client(lambda _r: httpx.Response(201, json={}))
        base = dict(
            operation_id=operation_id_for(symbol="XAUUSD", pair_id="ny:1", side="long"),
            occurred_at=OCCURRED,
            symbol="xauusd",
            direction="buy",
            entry_price=2700.5,
            stop_distance=22.1,
            target_distance=66.3,
            expires_at=OCCURRED + timedelta(hours=2),
        )
        base.update(over)
        return client.build_stop_entry(**base)  # type: ignore[arg-type]

    def test_shape_matches_the_gateway_contract(self) -> None:
        payload = self._payload()
        assert payload["execution_type"] == "stop"
        assert payload["direction"] == "buy"
        assert payload["instrument"] == "XAUUSD", "instrument is upper-cased"
        assert payload["entry_price"] == "2700.5"
        assert payload["source"] == "session_hedging"
        assert payload["targets"] == [{"account": "forex_demo", "volume_lots": "0.01"}]

    def test_protection_is_sent_as_distance_not_price(self) -> None:
        # The engine anchors an OCO stop and target to the actual fill, which only the
        # broker knows; absolute levels computed from a bar close would drift by the spread.
        payload = self._payload()
        assert payload["stop_loss_distance"] == "22.1"
        assert payload["take_profit_distance"] == "66.3"
        assert "stop_loss" not in payload
        assert "take_profit" not in payload

    def test_expiry_uses_gtd(self) -> None:
        payload = self._payload()
        assert payload["time_in_force"] == "gtd"
        assert payload["expires_at"] == "2026-01-05T16:00:00Z"

    def test_no_expiry_leaves_time_in_force_unset(self) -> None:
        payload = self._payload(expires_at=None)
        assert "time_in_force" not in payload
        assert "expires_at" not in payload


class TestResponseClassification:
    @pytest.mark.asyncio
    async def test_terminal_success(self) -> None:
        body = {
            "operation_id": "x",
            "state": "succeeded",
            "targets": [
                {
                    "account": "forex_demo",
                    "state": "filled",
                    "order_id": 71,
                    "position_id": 91,
                    "execution_price": "2700.7",
                }
            ],
        }
        client = _client(lambda _r: httpx.Response(201, json=body))
        result = await client.submit({})
        assert result.state is ExecutionState.SUCCEEDED
        assert result.order_ids == {"forex_demo": 71}
        assert result.fill_price("forex_demo") == pytest.approx(2700.7)

    @pytest.mark.asyncio
    async def test_202_is_pending(self) -> None:
        client = _client(lambda _r: httpx.Response(202, json={"state": "pending", "targets": []}))
        assert (await client.submit({})).state is ExecutionState.PENDING

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [409, 422])
    async def test_decisions_are_rejected_not_retryable(self, status: int) -> None:
        body = {"error": {"code": "source_not_allowed", "message": "no"}}
        client = _client(lambda _r: httpx.Response(status, json=body))
        result = await client.submit({})
        assert result.state is ExecutionState.REJECTED
        assert result.reason == "source_not_allowed"

    @pytest.mark.asyncio
    async def test_server_error_is_unknown_so_the_caller_reconciles(self) -> None:
        client = _client(
            lambda _r: httpx.Response(503, json={"error": {"code": "trading_disabled"}})
        )
        assert (await client.submit({})).state is ExecutionState.UNKNOWN

    @pytest.mark.asyncio
    async def test_transport_failure_is_unknown_never_rejected(self) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        result = await _client(boom).submit({})
        # The order may or may not have reached the broker. Treating this as rejected
        # would invite a resubmit and a duplicate position.
        assert result.state is ExecutionState.UNKNOWN
        assert result.reason == "ConnectError"

    @pytest.mark.asyncio
    async def test_missing_operation_is_not_found(self) -> None:
        client = _client(lambda _r: httpx.Response(404, json={"error": {"code": "gone"}}))
        state = (await client.get_operation(UUID(int=1))).state
        assert state is ExecutionState.NOT_FOUND


class TestAuthAndReadiness:
    @pytest.mark.asyncio
    async def test_api_key_header_is_sent(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(201, json={"state": "succeeded", "targets": []})

        await _client(handler).submit({})
        assert seen["x-api-key"] == "0123456789abcdef0123"

    @pytest.mark.asyncio
    async def test_trading_ready_reports_the_gateway_reason(self) -> None:
        body = {"status": "not_ready", "reason": "trading_enabled is false"}
        client = _client(lambda _r: httpx.Response(503, json=body))
        ready, reason = await client.trading_ready()
        assert ready is False
        assert reason == "trading_enabled is false"

    @pytest.mark.asyncio
    async def test_trading_ready_survives_an_unreachable_gateway(self) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        ready, _reason = await _client(boom).trading_ready()
        assert ready is False

    @pytest.mark.asyncio
    async def test_account_lists_return_bare_arrays(self) -> None:
        rows = [{"account": "forex_demo", "order_id": 71, "instrument": "XAUUSD"}]
        client = _client(lambda _r: httpx.Response(200, json=rows))
        assert await client.list_orders() == rows
