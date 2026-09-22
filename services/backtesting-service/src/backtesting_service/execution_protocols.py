"""Execution capabilities and segregated client interfaces.

Operation success describes a request; BrokerLifecycle describes the order or
position it created. They intentionally have independent lifetimes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from .execution import ExecutionResult


class BrokerLifecycle(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    DISPATCHING = "dispatching"
    UNKNOWN = "unknown"
    PLACED = "placed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    CLOSED = "closed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ExecutionCapabilities:
    market_entry: bool = False
    pending_entry: bool = False
    cancellation: bool = False
    inventory: bool = False
    protection_amendment: bool = False
    oco_coordination: bool = False


class ExecutionTransport(Protocol):
    @property
    def account(self) -> str: ...

    @property
    def source(self) -> str: ...

    async def submit(self, payload: dict[str, Any]) -> ExecutionResult: ...

    async def get_operation(self, operation_id: UUID) -> ExecutionResult: ...

    async def trading_ready(self) -> tuple[bool, str]: ...

    async def list_orders(self) -> list[dict[str, Any]]: ...

    async def list_positions(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class MarketEntryClient(Protocol):
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
    ) -> dict[str, Any]: ...


@runtime_checkable
class PendingEntryClient(Protocol):
    def build_stop_entry(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_distance: float,
        target_distance: float,
        expires_at: datetime | None = None,
        note: str | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class CancellationClient(Protocol):
    async def cancel_order(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        order_id: int,
    ) -> ExecutionResult: ...


@runtime_checkable
class ProtectionClient(Protocol):
    async def amend_protection(
        self,
        *,
        operation_id: UUID,
        occurred_at: datetime,
        position_id: int,
        stop_loss: float | None = None,
    ) -> ExecutionResult: ...


@runtime_checkable
class OcoGroupClient(Protocol):
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
    ) -> dict[str, Any]: ...

    async def submit_group(self, payload: dict[str, Any]) -> ExecutionResult: ...

    async def get_group(self, group_id: UUID) -> ExecutionResult: ...

    async def cancel_group(self, group_id: UUID, reason: str) -> ExecutionResult: ...
