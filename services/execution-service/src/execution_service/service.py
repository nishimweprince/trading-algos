"""One execution path for every broker.

`/v1/orders`, `/v1/orders/amend|cancel` and `/v1/positions/protection|close`
run through here whichever broker serves each target account. The service owns
the generic gates (source allowlist, freshness, TRADING_ENABLED), idempotency
and the ledger; each `ExecutionProvider` owns its broker's validation, request
shape and outcome mapping. A target's provider is found by its account alias:
a cTrader registry alias, or the MT5 host's profile.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel
from ta_contracts import (
    AmendOrderRequest,
    BrokerOrder,
    BrokerPosition,
    CancelOrderRequest,
    ClosePositionRequest,
    OperationAction,
    OperationResponse,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
    TargetState,
)
from ta_plugin_api import ExecutionProvider
from ta_store import ExecutionRepository, OperationConflictError

from .config import Settings
from .errors import ServiceError
from .logging_config import log_event

T = TypeVar("T")

_UNSETTLED = {
    TargetState.RESERVED,
    TargetState.DISPATCHED,
    TargetState.ACCEPTED,
    TargetState.PARTIALLY_FILLED,
}


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        providers: list[ExecutionProvider],
        repository: ExecutionRepository,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.repository = repository
        self._by_account: dict[str, ExecutionProvider] = {}
        for provider in providers:
            for account in provider.accounts():
                if account in self._by_account:
                    raise ValueError(
                        f"account alias {account!r} is served by both "
                        f"{self._by_account[account].name} and {provider.name}"
                    )
                self._by_account[account] = provider
            provider.attach_ledger(repository)

    # --- routes -----------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OperationResponse:
        return await self._execute(request, OperationAction.PLACE_ORDER)

    async def cancel_order(self, request: CancelOrderRequest) -> OperationResponse:
        return await self._execute(request, OperationAction.CANCEL_ORDER)

    async def amend_order(self, request: AmendOrderRequest) -> OperationResponse:
        return await self._execute(request, OperationAction.AMEND_ORDER)

    async def amend_position(self, request: PositionProtectionRequest) -> OperationResponse:
        return await self._execute(request, OperationAction.AMEND_POSITION)

    async def close_position(self, request: ClosePositionRequest) -> OperationResponse:
        return await self._execute(request, OperationAction.CLOSE_POSITION)

    def status(self, operation_id: UUID) -> OperationResponse:
        response = self.repository.get(operation_id)
        if response is None:
            raise ServiceError(404, "operation_not_found", "No operation has this ID")
        return response

    def orders(self, account: str) -> list[BrokerOrder]:
        return self._lookup(account, lambda provider: provider.orders(account))

    def positions(self, account: str) -> list[BrokerPosition]:
        return self._lookup(account, lambda provider: provider.positions(account))

    def accounts(self) -> tuple[str, ...]:
        return tuple(self._by_account)

    def account_statuses(self) -> list[dict[str, Any]]:
        return [status for provider in self.providers for status in provider.account_statuses()]

    async def reconcile(self) -> None:
        for provider in self.providers:
            await provider.reconcile()

    async def reconcile_unknown(self) -> None:
        """Periodic sweep: settle UNKNOWN targets the broker can now account for."""
        for provider in self.providers:
            try:
                await provider.reconcile_unknown()
            except Exception as exc:  # noqa: BLE001 - one provider must not stop the sweep
                log_event(
                    "unknown_reconcile_failed",
                    level=logging.WARNING,
                    provider=provider.name,
                    error=type(exc).__name__,
                )

    async def run_reconciler(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await self.reconcile_unknown()

    # --- internals --------------------------------------------------------------

    def _record_late_outcome(
        self, operation_id: UUID, account: str, dispatch: asyncio.Future[Any]
    ) -> None:
        """Write a dispatch outcome that arrived after the response timeout.

        Only over a target nothing else has settled: still in flight, or marked
        UNKNOWN by that timeout. Event-driven providers may have settled it
        already, and their answer wins.
        """
        if dispatch.cancelled() or dispatch.exception() is not None:
            return
        outcome = dispatch.result()
        current = self.repository.get(operation_id)
        target = next(
            (item for item in current.targets if item.account == account) if current else (),
            None,
        )
        if target is None:
            return
        timed_out = target.state is TargetState.UNKNOWN and target.error_code == "EXECUTION_TIMEOUT"
        if not timed_out and target.state not in {TargetState.RESERVED, TargetState.DISPATCHED}:
            return
        values = {"error_code": None, "error_message": None, **outcome.values}
        self.repository.update_target(
            operation_id, account, outcome.state, details=outcome.details, **values
        )
        log_event(
            "late_outcome_recorded",
            level=logging.WARNING,
            operation_id=str(operation_id),
            account=account,
            state=outcome.state.value,
        )

    def _lookup(self, account: str, read: Callable[[ExecutionProvider], T]) -> T:
        """A provider may resolve more than its aliases (cTrader also takes the
        numeric account ID), so an unmapped name is offered to each in turn."""
        provider = self._by_account.get(account)
        for candidate in [provider] if provider is not None else self.providers:
            try:
                return read(candidate)
            except KeyError:
                continue
        raise ServiceError(404, "account_not_found", f"Unknown or disabled account: {account}")

    def _provider_for(self, account: str) -> ExecutionProvider:
        provider = self._by_account.get(account)
        if provider is None:
            raise ServiceError(
                422,
                "account_not_allowed",
                f"Unknown or disabled account alias: {account}",
            )
        return provider

    async def _execute(self, request: Any, action: OperationAction) -> OperationResponse:
        self._validate_common(request)
        prepared: list[tuple[ExecutionProvider, str, str, Any]] = []
        for target in request.targets:
            provider = self._provider_for(target.account)
            client_order_id = provider.client_order_id(request.operation_id, target.account)
            message = await provider.prepare(action, request, target, client_order_id)
            prepared.append((provider, target.account, client_order_id, message))
        return await self._dispatch(request, action, prepared)

    async def _dispatch(
        self,
        request: Any,
        action: OperationAction,
        prepared: list[tuple[ExecutionProvider, str, str, Any]],
    ) -> OperationResponse:
        assert isinstance(request, BaseModel)
        payload_json = request.model_dump_json(exclude_none=False)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        operation_id: UUID = request.operation_id
        try:
            existing, created = self.repository.reserve(
                operation_id=operation_id,
                action=action,
                source=request.source,
                payload_hash=payload_hash,
                payload_json=payload_json,
                targets=[(account, client_order_id) for _, account, client_order_id, _ in prepared],
            )
        except OperationConflictError as exc:
            raise ServiceError(409, "operation_id_conflict", str(exc)) from exc
        if not created:
            return existing

        async def send(
            provider: ExecutionProvider, account: str, client_order_id: str, message: Any
        ) -> None:
            self.repository.update_target(operation_id, account, TargetState.DISPATCHED)
            dispatch = asyncio.ensure_future(
                provider.dispatch(operation_id, account, action, message, client_order_id)
            )
            try:
                outcome = await asyncio.shield(dispatch)
            except asyncio.CancelledError:
                # The response timeout fired with the broker call still running.
                # Let it finish and record what it returns, so a slow send does
                # not stay UNKNOWN for want of anyone listening.
                dispatch.add_done_callback(
                    lambda done: self._record_late_outcome(operation_id, account, done)
                )
                raise
            self.repository.update_target(
                operation_id, account, outcome.state, details=outcome.details, **outcome.values
            )

        tasks = [asyncio.create_task(send(*item)) for item in prepared]
        try:
            async with asyncio.timeout(self.settings.execution_response_timeout_seconds):
                await asyncio.gather(*tasks)
                while True:
                    current = self.repository.get(operation_id)
                    assert current is not None
                    if current.state is not OperationState.PENDING:
                        break
                    await asyncio.sleep(0.05)
        except TimeoutError:
            current = self.repository.get(operation_id)
            if current is not None:
                for target in current.targets:
                    if target.state in _UNSETTLED:
                        self.repository.update_target(
                            operation_id,
                            target.account,
                            TargetState.UNKNOWN,
                            error_code="EXECUTION_TIMEOUT",
                            error_message="Broker outcome requires reconciliation",
                        )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        response = self.repository.get(operation_id)
        assert response is not None
        log_event(
            "trade_operation_completed",
            level=(
                logging.WARNING
                if response.state
                in {
                    OperationState.PARTIAL_FAILURE,
                    OperationState.REJECTED,
                    OperationState.UNKNOWN,
                }
                else logging.INFO
            ),
            operation_id=str(operation_id),
            action=action.value,
            state=response.state.value,
            targets=[
                {
                    "account": target.account,
                    "state": target.state.value,
                    "error_code": target.error_code,
                }
                for target in response.targets
            ],
        )
        return response

    def _validate_common(self, request: Any) -> None:
        if request.source.strip().lower() not in self.settings.order_sources:
            raise ServiceError(422, "source_not_allowed", "Order source is not allowlisted")
        occurred_at = request.occurred_at.astimezone(UTC)
        now = datetime.now(UTC)
        age = (now - occurred_at).total_seconds()
        if age > self.settings.signal_max_age_seconds:
            raise ServiceError(422, "operation_too_old", "Operation exceeded its maximum age")
        if age < -self.settings.future_tolerance_seconds:
            raise ServiceError(422, "operation_from_future", "Operation timestamp is in the future")
        if not self.settings.trading_enabled:
            raise ServiceError(503, "trading_disabled", "TRADING_ENABLED is false")
