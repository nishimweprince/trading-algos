"""cTrader order execution over the ctrader-markets HTTP gateway.

Follows ``CandleStore``'s shape: the ``httpx.AsyncClient`` is owned by the app lifespan
and injected, auth is an ``X-API-Key`` header per request, and transport failures **raise**
rather than being swallowed. A silently dropped order submission is the worst failure mode
available here, so this deliberately does not follow ``Notifier``'s never-raise contract.

The gateway has no bracket or OCO concept: ``targets`` fans one order out across accounts,
it does not describe multiple legs. A straddle is two independent operations with two
``operation_id`` values, and the surviving side must be cancelled explicitly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid5

import httpx

from config import Settings

# Stable namespace for deriving an operation UUID from a structure's natural key. Fixed
# forever: changing it would make every in-flight operation look new to the gateway.
OPERATION_NAMESPACE = UUID("6f2a1d3c-8b74-4e59-9a10-2f5c7d8e4b16")

ORDERS_PATH = "/v1/orders"
CANCEL_PATH = "/v1/orders/cancel"
AMEND_PATH = "/v1/orders/amend"
PROTECTION_PATH = "/v1/positions/protection"
TRADING_READY_PATH = "/health/trading-ready"


class ExecutionState(StrEnum):
    """Normalised outcome of one gateway call."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"

    @property
    def terminal(self) -> bool:
        return self in {ExecutionState.SUCCEEDED, ExecutionState.REJECTED}


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    state: ExecutionState
    response: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def order_ids(self) -> dict[str, int]:
        """Broker order id per account alias. Cancel and amend need this, not operation_id."""
        if not self.response:
            return {}
        targets = self.response.get("targets")
        if not isinstance(targets, list):
            return {}
        found: dict[str, int] = {}
        for target in targets:
            if not isinstance(target, dict):
                continue
            alias, order_id = target.get("account"), target.get("order_id")
            if isinstance(alias, str) and isinstance(order_id, int):
                found[alias] = order_id
        return found

    def fill_price(self, alias: str) -> float | None:
        for target in (self.response or {}).get("targets", []):
            if isinstance(target, dict) and target.get("account") == alias:
                price = target.get("execution_price")
                if price is not None:
                    return float(price)
        return None


def decimal_text(value: float | Decimal, *, digits: int = 5) -> str:
    """Render a decimal without exponent notation.

    Load-bearing rather than cosmetic: the gateway hashes the whole submitted payload to
    detect a duplicate ``operation_id`` with a changed body, so ``1E-2`` and ``0.01`` are
    two different requests to it even though they are the same number.
    """
    quantised = round(Decimal(str(value)), digits)
    rendered = format(quantised, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def operation_id_for(*, symbol: str, pair_id: str, side: str, attempt: int = 0) -> UUID:
    """Deterministic operation id for one leg of one structure.

    Derived rather than random so a restart mid-submit recomputes the same id and the
    gateway's idempotency recognises the retry instead of opening a second position.
    """
    return uuid5(OPERATION_NAMESPACE, f"{symbol}|{pair_id}|{side}|{attempt}")


def client_order_id_for(operation_id: UUID, account: str) -> str:
    """Mirror of the gateway's own derivation, for re-attaching orphaned broker orders."""
    digest = hashlib.sha256(account.encode()).hexdigest()[:12]
    return f"{operation_id.hex}-{digest}"[:50]


def safe_reason(value: object, *, fallback: str) -> str:
    """Bounded, single-line reason text.

    ``None`` must fall through to the caller's fallback rather than stringifying into the
    literal "None", which is what a reader would otherwise see in the UI.
    """
    if value is None:
        return fallback
    text = str(value).strip().replace("\n", " ")
    return text[:240] if text else fallback


class ExecutionClient:
    """Thin, typed client for the ctrader-markets execution surface."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._base = settings.ctrader_markets_url.rstrip("/")
        self._timeout = settings.execution_timeout_seconds

    @property
    def account(self) -> str:
        return self._s.execution_account

    @property
    def source(self) -> str:
        return self._s.execution_source

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._s.ctrader_api_key
        if api_key is not None:
            headers["X-API-Key"] = api_key.get_secret_value()
        return headers

    def _envelope(self, operation_id: UUID, occurred_at: datetime) -> dict[str, Any]:
        return {
            "operation_id": str(operation_id),
            "occurred_at": timestamp_text(occurred_at),
            "source": self.source,
        }

    async def _call(
        self, method: Literal["GET", "POST"], path: str, payload: dict[str, Any] | None = None
    ) -> ExecutionResult:
        try:
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Transport failure: the order may or may not have reached the broker, so this
            # is UNKNOWN, never REJECTED. The caller must reconcile rather than resubmit.
            return ExecutionResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        return self._classify(response, method=method)

    @staticmethod
    def _classify(response: httpx.Response, *, method: str) -> ExecutionResult:
        if response.status_code == 404 and method == "GET":
            return ExecutionResult(ExecutionState.NOT_FOUND)
        try:
            body = response.json()
        except ValueError:
            return ExecutionResult(
                ExecutionState.UNKNOWN, reason=f"malformed response (HTTP {response.status_code})"
            )
        if not isinstance(body, dict):
            return ExecutionResult(ExecutionState.SUCCEEDED, response={"items": body})
        if not response.is_success:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            # 409 is an operation_id reused with a different payload and 422 is a rejected
            # shape; both are decisions, not outages, so they must not be retried blindly.
            rejected = response.status_code in {409, 422}
            return ExecutionResult(
                ExecutionState.REJECTED if rejected else ExecutionState.UNKNOWN,
                response=body,
                reason=safe_reason(error.get("code"), fallback=f"HTTP {response.status_code}"),
            )
        state = body.get("state")
        if response.status_code == 202 or state in {"pending", "unknown"}:
            return ExecutionResult(ExecutionState.PENDING, response=body)
        if state == "succeeded":
            return ExecutionResult(ExecutionState.SUCCEEDED, response=body)
        if state in {"partial_failure", "rejected"}:
            return ExecutionResult(ExecutionState.REJECTED, response=body, reason=str(state))
        return ExecutionResult(ExecutionState.SUCCEEDED, response=body)

    def build_stop_entry(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        symbol: str,
        direction: Literal["buy", "sell"],
        entry_price: float,
        stop_distance: float,
        target_distance: float,
        expires_at: datetime | None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Build the pending stop-entry payload without sending it.

        Split out from :meth:`submit_stop_entry` so shadow mode can produce, record and
        display the exact bytes that live mode would send.

        Protection is expressed as a *distance* rather than a level because the engine
        anchors an OCO structure's stop and target to the actual fill, which only the
        broker knows. Sending absolute prices computed from a bar close would silently
        drift by the spread and by whatever the price did between decision and trigger.
        """
        payload: dict[str, Any] = {
            **self._envelope(operation_id, occurred_at),
            "instrument": symbol.upper(),
            "execution_type": "stop",
            "direction": direction,
            "targets": [
                {
                    "account": self.account,
                    "volume_lots": decimal_text(self._s.execution_volume_lots),
                }
            ],
            "entry_price": decimal_text(entry_price),
            "stop_loss_distance": decimal_text(stop_distance),
            "take_profit_distance": decimal_text(target_distance),
        }
        if expires_at is not None:
            # GTD lets the broker expire an untriggered bracket on its own, so a missed
            # poll cannot leave one resting past OCO_EXPIRY_BARS.
            payload["time_in_force"] = "gtd"
            payload["expires_at"] = timestamp_text(expires_at)
        if note:
            payload["note"] = note[:500]
        return payload

    async def submit(self, payload: dict[str, Any]) -> ExecutionResult:
        return await self._call("POST", ORDERS_PATH, payload)

    async def cancel_order(
        self, *, operation_id: UUID, occurred_at: datetime, order_id: int
    ) -> ExecutionResult:
        return await self._call(
            "POST",
            CANCEL_PATH,
            {
                **self._envelope(operation_id, occurred_at),
                "targets": [{"account": self.account, "order_id": order_id}],
            },
        )

    async def amend_protection(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> ExecutionResult:
        """Move protection on an open position. Levels here are absolute, not distances."""
        target: dict[str, Any] = {"account": self.account, "position_id": position_id}
        if stop_loss is not None:
            target["stop_loss"] = decimal_text(stop_loss)
        if take_profit is not None:
            target["take_profit"] = decimal_text(take_profit)
        return await self._call(
            "POST",
            PROTECTION_PATH,
            {**self._envelope(operation_id, occurred_at), "targets": [target]},
        )

    async def get_operation(self, operation_id: UUID) -> ExecutionResult:
        return await self._call("GET", f"/v1/operations/{operation_id}")

    async def list_orders(self) -> list[dict[str, Any]]:
        result = await self._call("GET", f"/v1/accounts/{self.account}/orders")
        return _as_items(result)

    async def list_positions(self) -> list[dict[str, Any]]:
        result = await self._call("GET", f"/v1/accounts/{self.account}/positions")
        return _as_items(result)

    async def trading_ready(self) -> tuple[bool, str]:
        """Gateway readiness. Unauthenticated by design, like the other health routes."""
        try:
            response = await self._client.get(
                f"{self._base}{TRADING_READY_PATH}", timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return False, safe_reason(exc, fallback=f"{type(exc).__name__} contacting {self._base}")
        if response.status_code != 200:
            try:
                body = response.json()
            except ValueError:
                return False, f"HTTP {response.status_code}"
            reason = body.get("reason") or body.get("error", {}).get("code")
            return False, safe_reason(reason, fallback=f"HTTP {response.status_code}")
        return True, "ready"


def _as_items(result: ExecutionResult) -> list[dict[str, Any]]:
    """The account list routes return a bare JSON array, not an operation envelope."""
    if result.state is ExecutionState.NOT_FOUND or result.response is None:
        return []
    items = result.response.get("items", result.response)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
