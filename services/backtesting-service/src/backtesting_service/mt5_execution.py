"""MT5 signal client used by the HFM execution profile.

The MT5 adapter intentionally exposes the legacy, single-market-order
``/v1/signals`` contract rather than the cTrader ``/v1/orders`` surface. This
client normalises that response into the bridge's existing execution result so
restart reconciliation and failure halting remain shared.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import httpx

from .config import Settings
from .execution import (
    ExecutionResult,
    ExecutionState,
    decimal_text,
    safe_reason,
    timestamp_text,
)
from .execution_protocols import ExecutionCapabilities
from .models import Mt5OcoExecution


class Mt5ExecutionClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._base = settings.mt5_signal_api_url.rstrip("/")
        self._timeout = settings.execution_timeout_seconds

    @property
    def account(self) -> str:
        return self._s.execution_mt5_profile

    @property
    def source(self) -> str:
        return self._s.execution_source

    def _broker_symbol(self, symbol: str) -> str:
        if symbol.upper() == self._s.symbol.upper() and self._s.mt5_execution_symbol is not None:
            return self._s.mt5_execution_symbol
        return symbol.upper()

    @property
    def supports_broker_inventory(self) -> bool:
        return self._s.mt5_oco_execution is Mt5OcoExecution.BROKER_PENDING

    @property
    def capabilities(self) -> ExecutionCapabilities:
        broker = self.supports_broker_inventory
        return ExecutionCapabilities(
            market_entry=True,
            pending_entry=broker,
            cancellation=broker,
            inventory=broker,
            protection_amendment=broker,
            oco_coordination=broker,
        )

    def build_oco_group(
        self,
        *,
        group_id: UUID,
        occurred_at: datetime,
        decision_at: datetime,
        symbol: str,
        upper_trigger: float,
        lower_trigger: float,
        stop_distance: float,
        target_distance: float,
        expires_at: datetime,
    ) -> dict[str, Any]:
        return {
            "group_id": str(group_id),
            "profile": self.account,
            "occurred_at": timestamp_text(occurred_at),
            "decision_at": timestamp_text(decision_at),
            "symbol": self._broker_symbol(symbol),
            "volume": decimal_text(self._s.execution_volume_lots),
            "upper_trigger": decimal_text(upper_trigger),
            "lower_trigger": decimal_text(lower_trigger),
            "stop_distance": decimal_text(stop_distance),
            "target_distance": decimal_text(target_distance),
            "expires_at": timestamp_text(expires_at),
            "source": self.source,
            "protection_policy": "fill_relative",
        }

    async def _group_call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> ExecutionResult:
        try:
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                json=payload,
                params=params,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return ExecutionResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        if method == "GET" and response.status_code == 404:
            return ExecutionResult(ExecutionState.NOT_FOUND)
        try:
            body = response.json()
        except ValueError:
            return ExecutionResult(ExecutionState.UNKNOWN, reason="malformed OCO response")
        if not isinstance(body, dict):
            return ExecutionResult(ExecutionState.UNKNOWN, reason="malformed OCO response")
        if not response.is_success:
            error = body.get("error") or {}
            reason = error.get("code") if isinstance(error, dict) else None
            state = (
                ExecutionState.REJECTED
                if response.status_code in {401, 409, 422}
                else ExecutionState.UNKNOWN
            )
            return ExecutionResult(
                state,
                response=body,
                reason=safe_reason(reason, fallback=f"HTTP {response.status_code}"),
            )
        expected_id = str(payload["group_id"]) if payload is not None else path.split("/")[4]
        if not self._valid_group(body, expected_id):
            return ExecutionResult(ExecutionState.UNKNOWN, reason="malformed OCO lifecycle")
        state = ExecutionState.SUCCEEDED
        if body["state"] in {"unknown", "halted"} or body.get("fault"):
            state = ExecutionState.UNKNOWN
        elif body["state"] == "rejected":
            state = ExecutionState.REJECTED
        return ExecutionResult(state, response=body, reason=body.get("fault") or body.get("reason"))

    async def submit_group(self, payload: dict[str, Any]) -> ExecutionResult:
        result = await self._group_call("POST", "/v1/mt5/oco", payload)
        if result.state is ExecutionState.UNKNOWN:
            reconciled = await self.get_group(UUID(str(payload["group_id"])))
            if reconciled.state is not ExecutionState.NOT_FOUND:
                return reconciled
        return result

    async def get_group(self, group_id: UUID) -> ExecutionResult:
        return await self._group_call("GET", f"/v1/mt5/oco/{group_id}")

    async def cancel_group(self, group_id: UUID, reason: str) -> ExecutionResult:
        return await self._group_call(
            "POST", f"/v1/mt5/oco/{group_id}/cancel", params={"reason": reason}
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._s.mt5_signal_api_key is not None:
            headers["X-API-Key"] = self._s.mt5_signal_api_key.get_secret_value()
        return headers

    def build_market_entry(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        symbol: str,
        direction: str,
        stop_distance: float,
        target_distance: float,
        note: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "signal_id": str(operation_id),
            "occurred_at": timestamp_text(occurred_at),
            "execution_type": "market",
            "symbol": self._broker_symbol(symbol),
            "direction": direction,
            "volume": decimal_text(self._s.execution_volume_lots),
            "stop_loss_distance": decimal_text(stop_distance),
            "take_profit_distance": decimal_text(target_distance),
            "source": self.source,
            "ignore_signal_age": self._s.mt5_ignore_signal_age,
        }
        if self._s.mt5_deviation_points is not None:
            payload["deviation_points"] = self._s.mt5_deviation_points
        if note:
            payload["note"] = note[:500]
        return payload

    async def submit(self, payload: dict[str, Any]) -> ExecutionResult:
        signal_id = UUID(str(payload["signal_id"]))
        try:
            response = await self._client.post(
                f"{self._base}/v1/signals",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # The request may have reached MT5. Reconcile once and never blindly
            # resubmit a market entry after an ambiguous transport failure.
            reconciled = await self.get_operation(signal_id)
            if reconciled.state is not ExecutionState.NOT_FOUND:
                return reconciled
            return ExecutionResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        if response.status_code == 409:
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict) and error.get("code") == "signal_in_progress":
                return await self.get_operation(signal_id)
        return self._classify(response)

    async def get_operation(self, operation_id: UUID) -> ExecutionResult:
        try:
            response = await self._client.get(
                f"{self._base}/v1/signals/{operation_id}",
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return ExecutionResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        if response.status_code == 404:
            return ExecutionResult(ExecutionState.NOT_FOUND)
        return self._classify(response)

    def _classify(self, response: httpx.Response) -> ExecutionResult:
        try:
            body = response.json()
        except ValueError:
            return ExecutionResult(
                ExecutionState.UNKNOWN, reason=f"malformed response (HTTP {response.status_code})"
            )
        if not isinstance(body, dict):
            return ExecutionResult(ExecutionState.UNKNOWN, reason="malformed response")
        if not response.is_success:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = error.get("code")
            state = (
                ExecutionState.REJECTED
                if response.status_code in {401, 409, 422}
                else ExecutionState.UNKNOWN
            )
            return ExecutionResult(
                state,
                response=body,
                reason=safe_reason(code, fallback=f"HTTP {response.status_code}"),
            )

        state = body.get("state") or body.get("outcome")
        response_body = body.get("response") if isinstance(body.get("response"), dict) else body
        if state in {"received", "executing"}:
            return ExecutionResult(ExecutionState.PENDING, response=body)
        if state == "unknown":
            return ExecutionResult(ExecutionState.UNKNOWN, response=body, reason="unknown")
        if state == "rejected":
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            return ExecutionResult(
                ExecutionState.REJECTED,
                response=body,
                reason=safe_reason(error.get("code"), fallback="rejected"),
            )
        if state in {"filled", "partially_filled", "placed"}:
            return ExecutionResult(
                ExecutionState.SUCCEEDED,
                response=self._normalise_success(response_body, state=str(state)),
            )
        return ExecutionResult(ExecutionState.UNKNOWN, response=body, reason="unrecognised state")

    def _normalise_success(self, body: dict[str, Any], *, state: str) -> dict[str, Any]:
        target: dict[str, Any] = {"account": self.account, "broker_state": state}
        if isinstance(body.get("order_ticket"), int):
            target["order_id"] = body["order_ticket"]
        if body.get("execution_price") is not None:
            target["execution_price"] = body["execution_price"]
        if body.get("executed_volume") is not None:
            target["executed_volume"] = str(body["executed_volume"])
        return {"targets": [target], "mt5_signal": body}

    async def trading_ready(self) -> tuple[bool, str]:
        if self.supports_broker_inventory:
            return await self._oco_ready()
        try:
            response = await self._client.get(f"{self._base}/health/ready", timeout=self._timeout)
        except httpx.HTTPError as exc:
            return False, safe_reason(exc, fallback=type(exc).__name__)
        if response.status_code == 200:
            return True, "ready"
        try:
            body = response.json()
        except ValueError:
            return False, f"HTTP {response.status_code}"
        details = body.get("details") if isinstance(body, dict) else None
        reason = details.get("reason") if isinstance(details, dict) else None
        return False, safe_reason(reason, fallback=f"HTTP {response.status_code}")

    async def _oco_ready(self) -> tuple[bool, str]:
        try:
            response = await self._client.get(
                f"{self._base}/v1/mt5/capabilities",
                params={"symbol": self._broker_symbol(self._s.symbol)},
                headers=self._headers(),
                timeout=self._timeout,
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return False, type(exc).__name__
        if not response.is_success or not isinstance(body, dict):
            return False, "MT5 gateway OCO capability endpoint unavailable"
        capabilities = body.get("capabilities")
        required = {
            "pending_entry",
            "cancellation",
            "inventory",
            "protection_amendment",
            "oco_coordination",
        }
        if (
            body.get("version") != 1
            or body.get("profile") != self.account
            or not isinstance(capabilities, dict)
            or any(capabilities.get(key) is not True for key in required)
        ):
            return False, "MT5 gateway does not support the required OCO contract/profile"
        return body.get("ready") is True, safe_reason(
            body.get("reason"), fallback="OCO readiness unknown"
        )

    def _valid_group(self, body: dict[str, Any], expected_id: str) -> bool:
        if body.get("group_id") != expected_id or body.get("profile") != self.account:
            return False
        legs = body.get("legs")
        if not isinstance(legs, dict) or set(legs) != {"long", "short"}:
            return False
        valid_states = {
            "not_submitted",
            "dispatching",
            "unknown",
            "placed",
            "partially_filled",
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "closed",
        }
        try:
            for leg in legs.values():
                if not isinstance(leg, dict) or leg.get("state") not in valid_states:
                    return False
                UUID(str(leg["signal_id"]))
                volume = Decimal(str(leg["executed_volume"]))
                if not volume.is_finite() or volume < 0:
                    return False
                if leg.get("order_id") is not None and type(leg["order_id"]) is not int:
                    return False
                if not isinstance(leg.get("position_ids"), list):
                    return False
                if volume > 0 and (
                    not Decimal(str(leg["fill_price"])).is_finite()
                    or Decimal(str(leg["fill_price"])) <= 0
                ):
                    return False
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return False
        return body.get("state") in {
            "staging",
            "placed",
            "filled",
            "closed",
            "cancelling",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
            "halted",
        }

    async def _inventory(self) -> dict[str, Any]:
        response = await self._client.get(
            f"{self._base}/v1/mt5/inventory", headers=self._headers(), timeout=self._timeout
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("profile") != self.account:
            raise ValueError("MT5 inventory profile mismatch")
        return body

    async def list_orders(self) -> list[dict[str, Any]]:
        if not self.supports_broker_inventory:
            return []
        return [
            {
                "account": self.account,
                "order_id": row["ticket"],
                "instrument": row["symbol"],
                "volume_lots": str(row["volume_current"]),
                "state": "placed",
            }
            for row in (await self._inventory())["orders"]
        ]

    async def list_positions(self) -> list[dict[str, Any]]:
        if not self.supports_broker_inventory:
            return []
        return [
            {
                "account": self.account,
                "position_id": row["ticket"],
                "instrument": row["symbol"],
                "volume_lots": str(row["volume"]),
                "direction": "buy" if row["type"] == 0 else "sell",
                "price": str(row.get("price_open", "0")),
                "stop_loss": str(row["sl"]),
                "take_profit": str(row["tp"]),
            }
            for row in (await self._inventory())["positions"]
        ]
