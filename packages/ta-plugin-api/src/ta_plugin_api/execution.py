"""The execution half of a broker plugin.

One protocol, two very different brokers behind it:

- **cTrader is event-driven.** ``dispatch`` sends a request and returns the
  first event; later events (fills, cancels) settle the target through the
  ledger the service attached with ``attach_ledger``.
- **MetaTrader 5 is synchronous and blocking.** ``dispatch`` runs
  ``order_check``/``order_send`` in a thread under the terminal lock and returns
  the final outcome inline.

The ledger is the join point: ``TargetState`` runs RESERVED → DISPATCHED →
terminal, so the service never branches on which broker is behind a target.

Validation that depends on the broker (symbol metadata, volume steps, stops
levels, account permissions) belongs in ``prepare``, which runs before the
operation is reserved, so a rejected request leaves no ledger row. Generic gates
(source allowlist, freshness, TRADING_ENABLED) stay in the service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from ta_contracts import (
    BrokerOrder,
    BrokerPosition,
    OperationAction,
    OperationResponse,
    TargetState,
)

__all__ = [
    "ExecutionFactory",
    "ExecutionProvider",
    "LedgerPort",
    "TargetOutcome",
]


@dataclass(frozen=True)
class TargetOutcome:
    """What one dispatch did to one target.

    ``values`` holds the ledger columns to write (order_id, position_id, deal_id,
    executed_volume_lots, execution_price, error_code, error_message). A key that
    is present with ``None`` clears that column; a missing key leaves it alone.
    ``details`` is merged into the target's stored broker payloads.
    """

    state: TargetState
    values: Mapping[str, Any] = field(default_factory=dict)
    details: dict[str, Any] | None = None


class LedgerPort(Protocol):
    """The slice of ``ta_store.ExecutionRepository`` a provider may touch.

    Only event-driven providers need it: they settle targets whose outcome
    arrives after ``dispatch`` has returned.
    """

    def get(self, operation_id: UUID | str) -> OperationResponse | None: ...

    def find_by_client_order_id(self, client_order_id: str) -> tuple[str, str] | None: ...

    def update_target(
        self,
        operation_id: UUID | str,
        account: str,
        state: TargetState,
        *,
        details: dict[str, Any] | None = None,
        **values: Any,
    ) -> OperationResponse: ...

    def unresolved_targets(self, accounts: Iterable[str]) -> list[Any]:
        """Targets still RESERVED, DISPATCHED, ACCEPTED or UNKNOWN for these
        accounts, each with operation_id, account, client_order_id, state,
        broker_tag, details and created_at."""
        ...

    def append_event(
        self,
        *,
        account: str,
        event_type: str,
        payload: dict[str, Any],
        operation_id: UUID | str | None = None,
    ) -> None: ...


@runtime_checkable
class ExecutionProvider(Protocol):
    """One broker connection serving one or more account aliases."""

    name: str

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None: ...

    async def wait_ready(self, timeout_seconds: float) -> bool: ...

    async def close(self) -> None: ...

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """(ready, details). Details are surfaced verbatim on /health/*."""
        ...

    def attach_ledger(self, ledger: LedgerPort) -> None: ...

    async def reconcile(self) -> None:
        """Settle targets a restart left between dispatch and outcome."""
        ...

    async def reconcile_unknown(self) -> None:
        """Periodic and safe while orders are in flight: settle UNKNOWN targets
        the broker can now account for, leaving the rest UNKNOWN. Never touches
        RESERVED or DISPATCHED targets, which may belong to a live dispatch."""
        ...

    # --- accounts -----------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        """Account aliases this provider serves. Unique across providers."""
        ...

    def account_statuses(self) -> list[dict[str, Any]]: ...

    def orders(self, account: str) -> list[BrokerOrder]: ...

    def positions(self, account: str) -> list[BrokerPosition]: ...

    # --- execution ----------------------------------------------------------

    def client_order_id(self, operation_id: UUID, account: str) -> str:
        """The broker-visible correlation for one target, stable across restarts."""
        ...

    async def prepare(
        self,
        action: OperationAction,
        request: Any,
        target: Any,
        client_order_id: str,
    ) -> Any:
        """Validate one target and build its broker request; raise ServiceError
        to reject. ``request`` is the operation contract and ``target`` its
        entry for this account."""
        ...

    async def dispatch(
        self,
        operation_id: UUID,
        account: str,
        action: OperationAction,
        prepared: Any,
        client_order_id: str,
    ) -> TargetOutcome:
        """Send a prepared request. Never raises for a broker outcome: rejection
        and ambiguity come back as REJECTED and UNKNOWN outcomes."""
        ...


class ExecutionFactory(Protocol):
    name: str

    def missing_settings(self, settings: Any) -> list[str]: ...

    def execution(self, settings: Any, **overrides: Any) -> ExecutionProvider: ...
