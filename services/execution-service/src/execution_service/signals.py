"""The legacy `/v1/signals` contract, on the shared execution ledger.

ipda, signals-scrapper and lookup-trader POST single-order signals here, and
their request, response and error bodies are frozen. What changed underneath:

- the record lives in ta-store's ledger as a one-target PLACE_ORDER operation
  whose operation ID is the signal ID, on the account the MT5 host serves;
- its payload hash is still sha256 of ``SignalRequest.canonical_json()``, so a
  signal migrated from signals.db replays and conflicts exactly as before;
- the exact response or error body is stored with the operation, so a replay
  and ``GET /v1/signals/{id}`` return what the first call returned;
- the broker policy (symbol checks, protective-level rules, request shape,
  result normalization) is the MT5 plugin's, shared with `/v1/orders`.

Signal state is read off the target: RESERVED is RECEIVED, DISPATCHED is
EXECUTING, PARTIALLY_FILLED_FINAL is PARTIALLY_FILLED, the rest map by name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ta_contracts import (
    OperationAction,
    SignalRequest,
    SignalResponse,
    SignalState,
    SignalStatus,
    TargetState,
)
from ta_core import ServiceError
from ta_plugin_mt5.execution import (
    MT5Execution,
    PlaceIntent,
    SymbolContext,
    decimal_or_none,
    positive_int_or_none,
)
from ta_plugin_mt5.execution import broker_details as _broker_details
from ta_plugin_mt5.terminal import ConnectionSnapshot
from ta_store import ExecutionRepository, OperationConflictError, OperationRecord

from .config import Settings
from .logging_config import log_event
from .notifications import NotificationClient
from .signal_log import SignalFileLog

SIGNAL_STATES: dict[TargetState, SignalState] = {
    TargetState.RESERVED: SignalState.RECEIVED,
    TargetState.DISPATCHED: SignalState.EXECUTING,
    TargetState.FILLED: SignalState.FILLED,
    TargetState.PARTIALLY_FILLED_FINAL: SignalState.PARTIALLY_FILLED,
    TargetState.PLACED: SignalState.PLACED,
    TargetState.REJECTED: SignalState.REJECTED,
    TargetState.UNKNOWN: SignalState.UNKNOWN,
}
TARGET_STATES: dict[SignalState, TargetState] = {
    signal: target for target, signal in SIGNAL_STATES.items()
}

_TERMINAL_STATES = frozenset(
    {
        SignalState.FILLED,
        SignalState.PARTIALLY_FILLED,
        SignalState.PLACED,
        SignalState.REJECTED,
        SignalState.UNKNOWN,
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _file_log(
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    log_event(event, level=level, exc_info=exc_info, console=False, **fields)


@dataclass(frozen=True)
class SignalRecord:
    """One signal as the ledger holds it, in the legacy vocabulary."""

    signal_id: str
    payload_hash: str
    payload_json: str
    state: SignalState
    broker_tag: str | None
    request: dict[str, Any] | None
    check: dict[str, Any] | None
    result: dict[str, Any] | None
    response: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


def signal_record(record: OperationRecord) -> SignalRecord:
    operation = record.operation
    target = operation.targets[0]
    details = next(iter(record.details.values()), {}) if record.details else {}
    return SignalRecord(
        signal_id=str(operation.operation_id),
        payload_hash=record.payload_hash,
        payload_json=record.payload_json,
        # A /v1/orders state with no legacy name (amended, closed…) never
        # belongs to a signal; UNKNOWN is the honest reading if one is asked for.
        state=SIGNAL_STATES.get(target.state, SignalState.UNKNOWN),
        broker_tag=record.broker_tag,
        request=details.get("request"),
        check=details.get("check"),
        result=details.get("result"),
        response=record.response,
        error=record.error,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def _stored_error(error: ServiceError) -> dict[str, Any]:
    return {"status_code": error.status_code, **error.as_dict()}


def _intent(signal: SignalRequest, broker_tag: str) -> PlaceIntent:
    return PlaceIntent(
        symbol=signal.symbol,
        direction=signal.direction,
        execution_type=signal.execution_type,
        volume=signal.volume,
        broker_tag=broker_tag,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        stop_loss_distance=signal.stop_loss_distance,
        take_profit_distance=signal.take_profit_distance,
        deviation_points=signal.deviation_points,
        expires_at=signal.expires_at,
        log_fields={"signal_id": str(signal.signal_id)},
    )


def validate_signal_source(settings: Settings, signal: SignalRequest) -> None:
    if signal.source not in settings.allowed_signal_sources:
        raise ServiceError(
            422,
            "source_not_allowed",
            "The signal source is not in ALLOWED_SIGNAL_SOURCES",
            {
                "source": signal.source,
                "allowed": sorted(settings.allowed_signal_sources),
            },
        )


def validate_signal_freshness(settings: Settings, signal: SignalRequest) -> None:
    now = utc_now()
    occurred = signal.occurred_at.astimezone(UTC)
    age = (now - occurred).total_seconds()
    if not signal.ignore_signal_age and age > settings.signal_max_age_seconds:
        raise ServiceError(
            422,
            "stale_signal",
            "The signal is older than the configured maximum age",
            {"age_seconds": round(age, 3)},
        )
    if age < -settings.future_tolerance_seconds:
        raise ServiceError(
            422,
            "future_signal",
            "The signal timestamp is too far in the future",
            {"seconds_ahead": round(-age, 3)},
        )
    if signal.expires_at is not None and signal.expires_at.astimezone(UTC) <= now:
        raise ServiceError(422, "expired_order", "expires_at must be in the future")


class SignalService:
    def __init__(
        self,
        settings: Settings,
        provider: MT5Execution,
        repository: ExecutionRepository,
        signal_file_log: SignalFileLog | None = None,
        notification_client: NotificationClient | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.adapter = provider.adapter
        self.repository = repository
        self._signal_file_log = signal_file_log
        self._notification_client = notification_client
        self._stop_adjustments: dict[str, dict[str, Any]] = {}

    @property
    def account(self) -> str:
        return self.provider.account

    # --- the routes -------------------------------------------------------------

    async def execute(self, signal: SignalRequest) -> SignalResponse:
        signal_data = signal.model_dump(mode="json")
        _file_log("signal_received", signal_id=str(signal.signal_id), signal=signal_data)
        payload = signal.canonical_json()
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        signal_id = str(signal.signal_id)
        broker_tag = self._order_comment(signal)
        try:
            _, created = await asyncio.to_thread(
                self.repository.reserve,
                operation_id=signal.signal_id,
                action=OperationAction.PLACE_ORDER,
                source=signal.source,
                payload_hash=payload_hash,
                payload_json=payload,
                targets=[(self.account, None)],
                broker_tag=broker_tag,
            )
        except OperationConflictError as exc:
            stored = await asyncio.to_thread(self.get, signal_id)
            state = stored.state.value if stored is not None else None
            _file_log(
                "signal_idempotency_conflict",
                level=logging.WARNING,
                signal_id=signal_id,
                stored_payload_hash=stored.payload_hash if stored is not None else None,
                received_payload_hash=payload_hash,
                stored_state=state,
            )
            raise ServiceError(
                409,
                "idempotency_conflict",
                "The signal_id has already been used with a different payload",
                {"state": state},
            ) from exc
        stored = await asyncio.to_thread(self.get, signal_id)
        assert stored is not None
        _file_log(
            "signal_idempotency_reserved" if created else "signal_duplicate_received",
            signal_id=signal_id,
            payload_hash=payload_hash,
            broker_tag=broker_tag,
            stored_state=stored.state.value,
        )

        if not created:
            return self._replay(stored)

        _file_log("terminal_lock_waiting", signal_id=signal_id)
        async with self.provider.exclusive():
            try:
                return await asyncio.to_thread(self._execute_new, signal, broker_tag)
            finally:
                await self._finalize_new_signal(signal_id)

    def get(self, signal_id: str | UUID) -> SignalRecord | None:
        record = self.repository.record(signal_id)
        return None if record is None else signal_record(record)

    async def status(self, signal_id: UUID) -> SignalStatus:
        stored = await asyncio.to_thread(self.get, signal_id)
        if stored is None:
            raise ServiceError(404, "signal_not_found", "No signal exists with that ID")
        return SignalStatus(
            signal_id=UUID(stored.signal_id),
            state=stored.state,
            response=(
                SignalResponse.model_validate(stored.response)
                if stored.response is not None
                else None
            ),
            error=stored.error,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
        )

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        database_ok = await asyncio.to_thread(self.repository.is_healthy)
        if not self.settings.trading_enabled:
            _file_log(
                "readiness_checked",
                ready=False,
                database=database_ok,
                trading_enabled=False,
            )
            return False, {"database": database_ok, "trading_enabled": False}
        try:
            connection = await asyncio.to_thread(self.adapter.connection_snapshot)
        except Exception as exc:
            _file_log(
                "readiness_check_failed",
                level=logging.ERROR,
                exc_info=True,
                database=database_ok,
                reason=type(exc).__name__,
            )
            return False, {
                "database": database_ok,
                "terminal_connected": False,
                "reason": type(exc).__name__,
            }
        ready = self._connection_ready(connection) and database_ok
        details = {
            "database": database_ok,
            **self.provider.connection_details(connection),
            "trading_enabled": self.settings.trading_enabled,
        }
        _file_log("readiness_checked", ready=ready, **details)
        return ready, details

    # --- startup ----------------------------------------------------------------

    def reconcile_startup(self) -> None:
        """Settle signals a restart left mid-flight.

        RECEIVED never reached the terminal, so it is rejected. EXECUTING may
        have: only the terminal's order and deal history can say, matched on
        the order comment (the signal source), symbol and volume.
        """
        _file_log("startup_reconciliation_started")
        pending = [
            target
            for target in self.repository.unresolved_targets([self.account])
            if target.client_order_id is None and target.broker_tag is not None
        ]
        restart_error = {
            "status_code": 409,
            "code": "restart_before_execution",
            "message": "The service restarted before this signal reached the broker",
        }
        received = [target for target in pending if target.state is TargetState.RESERVED]
        for target in received:
            self._mark_rejected(target.operation_id, restart_error)
            _file_log(
                "startup_received_signal_rejected",
                level=logging.WARNING,
                signal_id=target.operation_id,
                error=restart_error,
            )

        executing = [target for target in pending if target.state is TargetState.DISPATCHED]
        if not executing:
            _file_log(
                "startup_reconciliation_completed",
                received_rejected=len(received),
                executing_found=0,
            )
            return
        _file_log(
            "startup_executions_found",
            count=len(executing),
            signal_ids=[target.operation_id for target in executing],
        )
        try:
            connection = self.adapter.connection_snapshot()
            if not self._connection_ready(connection):
                raise RuntimeError("terminal is not ready for reconciliation")
            start = min(target.created_at for target in executing) - timedelta(minutes=5)
            end = datetime.now(UTC) + timedelta(minutes=1)
            deals = self.adapter.history_deals(start, end)
            orders = self.adapter.history_orders(start, end)
        except Exception as exc:
            error = {
                "status_code": 409,
                "code": "execution_outcome_unknown",
                "message": "Interrupted execution could not be reconciled",
                "details": {"reason": type(exc).__name__},
            }
            for target in executing:
                self._mark_unknown(target.operation_id, error)
                _file_log(
                    "startup_execution_reconciliation_failed",
                    level=logging.ERROR,
                    signal_id=target.operation_id,
                    error=error,
                    reason=type(exc).__name__,
                )
            return

        for target in executing:
            assert target.broker_tag is not None
            request = (target.details or {}).get("request")
            matched = self.provider.match_history(target.broker_tag, request, deals, orders)
            if matched is None:
                error = {
                    "status_code": 409,
                    "code": "execution_outcome_unknown",
                    "message": "No matching MT5 order or deal was found after restart",
                }
                self._mark_unknown(target.operation_id, error)
                _file_log(
                    "startup_execution_not_found",
                    level=logging.WARNING,
                    signal_id=target.operation_id,
                    broker_tag=target.broker_tag,
                    error=error,
                )
                continue
            outcome, item = matched
            response = SignalResponse(
                signal_id=UUID(target.operation_id),
                outcome=SIGNAL_STATES[outcome.state],
                order_ticket=outcome.values.get("order_id"),
                deal_ticket=outcome.values.get("deal_id"),
                executed_volume=outcome.values.get("executed_volume_lots"),
                execution_price=outcome.values.get("execution_price"),
                broker_comment=str(item.get("comment", "")) or None,
                processed_at=utc_now(),
                reconciled=True,
            )
            self._mark_success(target.operation_id, response, outcome.details)
            _file_log(
                "startup_execution_reconciled",
                signal_id=target.operation_id,
                response=response.model_dump(mode="json"),
            )
        _file_log(
            "startup_reconciliation_completed",
            received_rejected=len(received),
            executing_found=len(executing),
        )

    # --- one new signal ---------------------------------------------------------

    def _execute_new(self, signal: SignalRequest, broker_tag: str) -> SignalResponse:
        signal_id = str(signal.signal_id)
        _file_log("terminal_lock_acquired", signal_id=signal_id)
        request: dict[str, Any] | None = None
        check: dict[str, Any] | None = None
        _file_log("signal_validation_started", signal_id=signal_id)
        try:
            self._validate_source(signal)
            self._validate_freshness(signal)
            _file_log("signal_freshness_validated", signal_id=signal_id)
            self._ensure_ready()
            _file_log("terminal_readiness_validated", signal_id=signal_id)
            intent = _intent(signal, broker_tag)
            context = self._context(intent)
            _file_log(
                "symbol_context_validated",
                signal_id=signal_id,
                symbol=asdict(context.symbol),
                tick=asdict(context.tick),
            )
            request = self.provider.build_request(intent, context)
            request_diagnostics = self._request_diagnostics(request)
            _file_log(
                "mt5_request_prepared",
                signal_id=signal_id,
                request=request,
                request_diagnostics=request_diagnostics,
            )
            _file_log(
                "mt5_order_check_started",
                signal_id=signal_id,
                request=request,
                request_diagnostics=request_diagnostics,
            )
            check_started = time.perf_counter()
            check = self.adapter.order_check(request)
            check_elapsed_ms = round((time.perf_counter() - check_started) * 1000, 3)
            check_last_error = self.provider.safe_last_error() if check is None else None
            _file_log(
                "mt5_order_check_completed",
                signal_id=signal_id,
                check=check,
                elapsed_ms=check_elapsed_ms,
                request_diagnostics=request_diagnostics,
                last_error=check_last_error,
            )
            if check is None:
                _file_log(
                    "mt5_order_check_returned_none",
                    level=logging.WARNING,
                    signal_id=signal_id,
                    request=request,
                    request_diagnostics=request_diagnostics,
                    elapsed_ms=check_elapsed_ms,
                    last_error=check_last_error,
                )
                raise ServiceError(
                    503,
                    "mt5_preflight_unavailable",
                    "MT5 did not return a preflight result",
                    {
                        "last_error": check_last_error,
                        "request_diagnostics": request_diagnostics,
                    },
                )
            if int(check.get("retcode", -1)) != 0:
                raise ServiceError(
                    422,
                    "preflight_rejected",
                    "MT5 rejected the order during preflight",
                    _broker_details(check),
                )
            _file_log("mt5_preflight_accepted", signal_id=signal_id, check=check)
        except ServiceError as exc:
            self._mark_rejected(signal_id, _stored_error(exc), request=request, check=check)
            _file_log(
                "signal_rejected_before_execution",
                level=logging.WARNING,
                signal_id=signal_id,
                error=_stored_error(exc),
                request=request,
                check=check,
            )
            raise
        except Exception as exc:
            error = ServiceError(
                503,
                "mt5_validation_unavailable",
                "MT5 validation failed before order submission",
                {"reason": type(exc).__name__},
            )
            self._mark_rejected(signal_id, _stored_error(error), request=request, check=check)
            _file_log(
                "signal_validation_failed_unexpectedly",
                level=logging.ERROR,
                exc_info=True,
                signal_id=signal_id,
                error=_stored_error(error),
                request=request,
                check=check,
            )
            raise error from exc

        assert request is not None and check is not None
        self.repository.update_target(
            signal_id,
            self.account,
            TargetState.DISPATCHED,
            details={"request": request, "check": check},
        )
        _file_log("signal_marked_executing", signal_id=signal_id, request=request, check=check)
        try:
            _file_log(
                "mt5_order_send_started",
                signal_id=signal_id,
                request=request,
                request_diagnostics=self._request_diagnostics(request),
            )
            send_started = time.perf_counter()
            result = self.adapter.order_send(request)
        except Exception as exc:
            error = ServiceError(
                503,
                "execution_outcome_unknown",
                "MT5 raised an error after order submission began; the request was not retried",
                {"reason": type(exc).__name__},
            )
            self._mark_unknown(signal_id, _stored_error(error))
            _file_log(
                "mt5_order_send_failed",
                level=logging.ERROR,
                exc_info=True,
                signal_id=signal_id,
                error=_stored_error(error),
                request=request,
            )
            raise error from exc

        _file_log(
            "mt5_order_send_completed",
            signal_id=signal_id,
            result=result,
            elapsed_ms=round((time.perf_counter() - send_started) * 1000, 3),
            request_diagnostics=self._request_diagnostics(request),
            last_error=self.provider.safe_last_error() if result is None else None,
        )

        if result is None:
            error = ServiceError(
                503,
                "execution_outcome_unknown",
                "MT5 returned no order result; the request was not retried",
                {"last_error": self.provider.safe_last_error()},
            )
            self._mark_unknown(signal_id, _stored_error(error))
            _file_log(
                "execution_outcome_unknown",
                level=logging.ERROR,
                signal_id=signal_id,
                error=_stored_error(error),
                request=request,
            )
            raise error

        response = self._normalize_result(signal, result)
        if response is None:
            error = ServiceError(
                422,
                "broker_rejected",
                "The broker rejected the order",
                _broker_details(result),
            )
            self._mark_rejected(signal_id, _stored_error(error), result=result)
            _file_log(
                "broker_execution_rejected",
                level=logging.WARNING,
                signal_id=signal_id,
                error=_stored_error(error),
                request=request,
                check=check,
                result=result,
            )
            raise error

        self._mark_success(signal_id, response, {"result": result})
        _file_log(
            "signal_execution_completed",
            signal_id=signal_id,
            request=request,
            check=check,
            result=result,
            response=response.model_dump(mode="json"),
        )
        return response

    async def _finalize_new_signal(self, signal_id: str) -> None:
        stored = await asyncio.to_thread(self.get, signal_id)
        if stored is None or stored.state not in _TERMINAL_STATES:
            return

        request_payload = json.loads(stored.payload_json)
        outcome = stored.response.get("outcome") if stored.response else None
        summary: dict[str, Any] = {
            "signal_id": signal_id,
            "profile": self.settings.profile,
            "symbol": request_payload.get("symbol"),
            "direction": request_payload.get("direction"),
            "volume": str(request_payload.get("volume")),
            "state": stored.state.value,
            "signal_source": request_payload.get("source"),
            "outcome": outcome,
            "error": stored.error,
        }
        stop_adjustments = self._stop_adjustments.pop(signal_id, None)
        if stop_adjustments:
            summary["stop_adjustments"] = stop_adjustments

        if self._signal_file_log is not None:
            self._signal_file_log.append(summary)

        log_event(
            "signal_post",
            signal_id=signal_id,
            symbol=summary["symbol"],
            direction=summary["direction"],
            state=summary["state"],
            outcome=outcome,
            profile=summary["profile"],
            signal_source=summary["signal_source"],
            error=summary["error"],
        )

        if self._notification_client is not None:
            await self._notification_client.notify_signal_outcome(summary)

    def _replay(self, stored: SignalRecord) -> SignalResponse:
        if stored.response is not None:
            _file_log(
                "signal_replay_returned_stored_response",
                signal_id=stored.signal_id,
                state=stored.state.value,
                response=stored.response,
            )
            return SignalResponse.model_validate(stored.response)
        if stored.error is not None:
            _file_log(
                "signal_replay_returned_stored_error",
                level=logging.WARNING,
                signal_id=stored.signal_id,
                state=stored.state.value,
                error=stored.error,
            )
            raise ServiceError(
                int(stored.error.get("status_code", 409)),
                str(stored.error.get("code", "signal_not_executable")),
                str(stored.error.get("message", "The stored signal cannot be executed")),
                stored.error.get("details"),
            )
        _file_log(
            "signal_replay_still_in_progress",
            level=logging.WARNING,
            signal_id=stored.signal_id,
            state=stored.state.value,
        )
        raise ServiceError(
            409,
            "signal_in_progress",
            "The signal is already being processed and will not be submitted twice",
            {"state": stored.state.value},
        )

    # --- ledger writes ----------------------------------------------------------

    def _mark_rejected(
        self,
        signal_id: str,
        error: dict[str, Any],
        *,
        request: dict[str, Any] | None = None,
        check: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        details = {
            key: value
            for key, value in (("request", request), ("check", check), ("result", result))
            if value is not None
        }
        self.repository.update_target(
            signal_id,
            self.account,
            TargetState.REJECTED,
            details=details or None,
            error_code=str(error.get("code")),
            error_message=str(error.get("message")),
        )
        self.repository.set_outcome(signal_id, error=error)

    def _mark_unknown(self, signal_id: str, error: dict[str, Any]) -> None:
        self.repository.update_target(
            signal_id,
            self.account,
            TargetState.UNKNOWN,
            error_code=str(error.get("code")),
            error_message=str(error.get("message")),
        )
        self.repository.set_outcome(signal_id, error=error)

    def _mark_success(
        self, signal_id: str, response: SignalResponse, details: dict[str, Any] | None
    ) -> None:
        self.repository.update_target(
            signal_id,
            self.account,
            TARGET_STATES[response.outcome],
            details=details,
            order_id=response.order_ticket,
            deal_id=response.deal_ticket,
            executed_volume_lots=response.executed_volume,
            execution_price=response.execution_price,
            error_code=None,
            error_message=None,
        )
        self.repository.set_outcome(signal_id, response=response.model_dump(mode="json"))

    # --- policy -----------------------------------------------------------------

    def _validate_source(self, signal: SignalRequest) -> None:
        validate_signal_source(self.settings, signal)

    def _validate_freshness(self, signal: SignalRequest) -> None:
        validate_signal_freshness(self.settings, signal)

    def _ensure_ready(self) -> None:
        self.provider.ensure_ready()

    def _connection_ready(self, connection: ConnectionSnapshot) -> bool:
        return self.provider.connection_ready(connection)

    def _context(self, intent: PlaceIntent) -> SymbolContext:
        context = self.provider.symbol_context(intent)
        if context.adjustments:
            self._stop_adjustments[str(intent.log_fields["signal_id"])] = context.adjustments
        return context

    def _normalize_result(
        self, signal: SignalRequest, result: dict[str, Any]
    ) -> SignalResponse | None:
        outcome = self.provider.place_outcome(result)
        if outcome is None:
            return None
        return SignalResponse(
            signal_id=signal.signal_id,
            outcome=SIGNAL_STATES[outcome.state],
            order_ticket=positive_int_or_none(result.get("order")),
            deal_ticket=positive_int_or_none(result.get("deal")),
            executed_volume=decimal_or_none(result.get("volume")),
            execution_price=decimal_or_none(result.get("price")),
            broker_retcode=int(result.get("retcode", -1)),
            broker_comment=str(result.get("comment", "")) or None,
            processed_at=utc_now(),
        )

    @staticmethod
    def _order_comment(signal: SignalRequest) -> str:
        return signal.source

    @staticmethod
    def _request_diagnostics(request: dict[str, Any]) -> dict[str, Any]:
        comment = request.get("comment")
        comment_text = comment if isinstance(comment, str) else None
        return {
            "field_types": {key: type(value).__name__ for key, value in sorted(request.items())},
            "comment": {
                "value": comment_text,
                "character_length": len(comment_text) if comment_text is not None else None,
                "utf8_byte_length": (
                    len(comment_text.encode("utf-8")) if comment_text is not None else None
                ),
                "ascii": comment_text.isascii() if comment_text is not None else None,
                "type": type(comment).__name__,
            },
        }
