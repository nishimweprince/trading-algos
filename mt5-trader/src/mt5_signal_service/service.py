from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from .config import Settings
from .errors import ServiceError
from .models import (
    Direction,
    ExecutionType,
    SignalRequest,
    SignalResponse,
    SignalState,
    SignalStatus,
    utc_now,
)
from .mt5_adapter import ConnectionSnapshot, MT5Adapter, SymbolSnapshot, TickSnapshot
from .repository import SignalRepository, StoredSignal


class SignalExecutionService:
    def __init__(
        self,
        settings: Settings,
        adapter: MT5Adapter,
        repository: SignalRepository,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.repository = repository
        self._terminal_lock = asyncio.Lock()

    async def execute(self, signal: SignalRequest) -> SignalResponse:
        payload = signal.canonical_json()
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        signal_id = str(signal.signal_id)
        broker_tag = self._broker_tag(signal.signal_id)
        stored, created = await asyncio.to_thread(
            self.repository.reserve, signal_id, payload_hash, payload, broker_tag
        )

        if not created:
            return self._replay(stored, payload_hash)

        async with self._terminal_lock:
            return await asyncio.to_thread(self._execute_new, signal, broker_tag)

    async def status(self, signal_id: UUID) -> SignalStatus:
        stored = await asyncio.to_thread(self.repository.get, str(signal_id))
        if stored is None:
            raise ServiceError(404, "signal_not_found", "No signal exists with that ID")
        return self._status_from_record(stored)

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        database_ok = await asyncio.to_thread(self.repository.is_healthy)
        if not self.settings.trading_enabled:
            return False, {"database": database_ok, "trading_enabled": False}
        try:
            connection = await asyncio.to_thread(self.adapter.connection_snapshot)
        except Exception as exc:
            return False, {
                "database": database_ok,
                "terminal_connected": False,
                "reason": type(exc).__name__,
            }
        ready = self._connection_ready(connection) and database_ok
        return ready, {
            "database": database_ok,
            "terminal_connected": connection.connected,
            "account_matches": connection.login == self.settings.login,
            "trade_allowed": connection.trade_allowed,
            "expert_allowed": connection.expert_allowed,
            "trading_enabled": self.settings.trading_enabled,
        }

    def reconcile_startup(self) -> None:
        restart_error = {
            "status_code": 409,
            "code": "restart_before_execution",
            "message": "The service restarted before this signal reached the broker",
        }
        for record in self.repository.list_states(SignalState.RECEIVED):
            self.repository.mark_rejected(record.signal_id, restart_error)

        executing = self.repository.list_states(SignalState.EXECUTING)
        if not executing:
            return
        try:
            connection = self.adapter.connection_snapshot()
            if not self._connection_ready(connection):
                raise RuntimeError("terminal is not ready for reconciliation")
            start = min(record.created_at for record in executing) - timedelta(minutes=5)
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
            for record in executing:
                self.repository.mark_unknown(record.signal_id, error)
            return

        for record in executing:
            response = self._find_history_result(record, deals, orders)
            if response is None:
                self.repository.mark_unknown(
                    record.signal_id,
                    {
                        "status_code": 409,
                        "code": "execution_outcome_unknown",
                        "message": "No matching MT5 order or deal was found after restart",
                    },
                )
            else:
                self.repository.mark_success(
                    record.signal_id,
                    response.outcome,
                    response.model_dump(mode="json"),
                )

    def _execute_new(self, signal: SignalRequest, broker_tag: str) -> SignalResponse:
        signal_id = str(signal.signal_id)
        request: dict[str, Any] | None = None
        check: dict[str, Any] | None = None
        try:
            self._validate_freshness(signal)
            self._ensure_ready()
            symbol, tick = self._symbol_context(signal)
            request = self._build_request(signal, broker_tag, symbol, tick)
            check = self.adapter.order_check(request)
            if check is None:
                raise ServiceError(
                    503,
                    "mt5_preflight_unavailable",
                    "MT5 did not return a preflight result",
                    {"last_error": self._safe_last_error()},
                )
            if int(check.get("retcode", -1)) != 0:
                raise ServiceError(
                    422,
                    "preflight_rejected",
                    "MT5 rejected the order during preflight",
                    self._broker_details(check),
                )
        except ServiceError as exc:
            self.repository.mark_rejected(
                signal_id,
                self._stored_error(exc),
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
            self.repository.mark_rejected(
                signal_id,
                self._stored_error(error),
                request=request,
                check=check,
            )
            raise error from exc

        assert request is not None and check is not None
        self.repository.mark_executing(signal_id, request, check)
        try:
            result = self.adapter.order_send(request)
        except Exception as exc:
            error = ServiceError(
                503,
                "execution_outcome_unknown",
                "MT5 raised an error after order submission began; the request was not retried",
                {"reason": type(exc).__name__},
            )
            self.repository.mark_unknown(signal_id, self._stored_error(error))
            raise error from exc

        if result is None:
            error = ServiceError(
                503,
                "execution_outcome_unknown",
                "MT5 returned no order result; the request was not retried",
                {"last_error": self._safe_last_error()},
            )
            self.repository.mark_unknown(signal_id, self._stored_error(error))
            raise error

        response = self._normalize_result(signal, result)
        if response is None:
            error = ServiceError(
                422,
                "broker_rejected",
                "The broker rejected the order",
                self._broker_details(result),
            )
            self.repository.mark_rejected(
                signal_id,
                self._stored_error(error),
                result=result,
            )
            raise error

        self.repository.mark_success(
            signal_id,
            response.outcome,
            response.model_dump(mode="json"),
            result=result,
        )
        return response

    def _replay(self, stored: StoredSignal, payload_hash: str) -> SignalResponse:
        if stored.payload_hash != payload_hash:
            raise ServiceError(
                409,
                "idempotency_conflict",
                "The signal_id has already been used with a different payload",
                {"state": stored.state.value},
            )
        if stored.response is not None:
            return SignalResponse.model_validate(stored.response)
        if stored.error is not None:
            raise ServiceError(
                int(stored.error.get("status_code", 409)),
                str(stored.error.get("code", "signal_not_executable")),
                str(stored.error.get("message", "The stored signal cannot be executed")),
                stored.error.get("details"),
            )
        raise ServiceError(
            409,
            "signal_in_progress",
            "The signal is already being processed and will not be submitted twice",
            {"state": stored.state.value},
        )

    def _validate_freshness(self, signal: SignalRequest) -> None:
        now = utc_now()
        occurred = signal.occurred_at.astimezone(UTC)
        age = (now - occurred).total_seconds()
        if age > self.settings.signal_max_age_seconds:
            raise ServiceError(
                422,
                "stale_signal",
                "The signal is older than the configured maximum age",
                {"age_seconds": round(age, 3)},
            )
        if age < -self.settings.future_tolerance_seconds:
            raise ServiceError(
                422,
                "future_signal",
                "The signal timestamp is too far in the future",
                {"seconds_ahead": round(-age, 3)},
            )
        if signal.expires_at is not None and signal.expires_at.astimezone(UTC) <= now:
            raise ServiceError(422, "expired_order", "expires_at must be in the future")

    def _ensure_ready(self) -> None:
        if not self.settings.trading_enabled:
            raise ServiceError(503, "trading_disabled", "Live trading is disabled by configuration")
        connection = self.adapter.connection_snapshot()
        if not self._connection_ready(connection):
            raise ServiceError(
                503,
                "terminal_not_ready",
                "The configured MT5 terminal and account are not ready for trading",
                {
                    "connected": connection.connected,
                    "account_matches": connection.login == self.settings.login,
                    "trade_allowed": connection.trade_allowed,
                    "expert_allowed": connection.expert_allowed,
                },
            )

    def _connection_ready(self, connection: ConnectionSnapshot) -> bool:
        return bool(
            connection.connected
            and connection.login == self.settings.login
            and connection.trade_allowed
            and connection.expert_allowed
        )

    def _symbol_context(self, signal: SignalRequest) -> tuple[SymbolSnapshot, TickSnapshot]:
        if signal.symbol not in self.settings.allowed_symbols:
            raise ServiceError(422, "symbol_not_allowed", "The symbol is not in ALLOWED_SYMBOLS")
        if signal.volume > self.settings.maximum_volume:
            raise ServiceError(
                422,
                "volume_cap_exceeded",
                "The requested volume exceeds MAXIMUM_VOLUME",
            )
        deviation = signal.deviation_points
        if deviation is not None and deviation > self.settings.maximum_deviation_points:
            raise ServiceError(
                422,
                "deviation_cap_exceeded",
                "deviation_points exceeds MAXIMUM_DEVIATION_POINTS",
            )

        symbol = self.adapter.symbol_info(signal.symbol)
        if symbol is None:
            raise ServiceError(422, "symbol_not_found", "The broker does not expose this symbol")
        if not symbol.visible:
            if not self.adapter.symbol_select(signal.symbol):
                raise ServiceError(422, "symbol_unavailable", "The symbol could not be enabled")
            symbol = self.adapter.symbol_info(signal.symbol)
            if symbol is None or not symbol.visible:
                raise ServiceError(
                    422,
                    "symbol_unavailable",
                    "The symbol is not visible after selection",
                )

        tick = self.adapter.symbol_tick(signal.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            raise ServiceError(503, "tick_unavailable", "A valid current bid/ask is unavailable")

        self._validate_volume(signal.volume, symbol)
        self._validate_prices(signal, symbol, tick)
        return symbol, tick

    @staticmethod
    def _validate_volume(volume: Decimal, symbol: SymbolSnapshot) -> None:
        minimum = Decimal(str(symbol.volume_min))
        maximum = Decimal(str(symbol.volume_max))
        step = Decimal(str(symbol.volume_step))
        if volume < minimum or volume > maximum:
            raise ServiceError(
                422,
                "invalid_volume",
                "Volume is outside the broker's symbol limits",
                {"minimum": str(minimum), "maximum": str(maximum)},
            )
        if step <= 0:
            raise ServiceError(503, "invalid_symbol_metadata", "Broker volume_step is invalid")
        try:
            increments = (volume - minimum) / step
        except InvalidOperation as exc:  # pragma: no cover - defensive decimal guard
            raise ServiceError(422, "invalid_volume", "Volume cannot be validated") from exc
        if increments != increments.to_integral_value():
            raise ServiceError(
                422,
                "invalid_volume_step",
                "Volume does not align with the broker's volume step",
                {"minimum": str(minimum), "step": str(step)},
            )

    def _validate_prices(
        self, signal: SignalRequest, symbol: SymbolSnapshot, tick: TickSnapshot
    ) -> None:
        for name, price in (
            ("entry_price", signal.entry_price),
            ("stop_loss", signal.stop_loss),
            ("take_profit", signal.take_profit),
        ):
            if price is not None:
                self._validate_precision(name, price, symbol.digits)

        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        entry = signal.entry_price
        if signal.execution_type is ExecutionType.LIMIT:
            valid = entry < ask if signal.direction is Direction.BUY else entry > bid
            if not valid:
                raise ServiceError(
                    422,
                    "invalid_entry_price",
                    "A buy limit must be below ask and a sell limit must be above bid",
                )
        elif signal.execution_type is ExecutionType.STOP:
            valid = entry > ask if signal.direction is Direction.BUY else entry < bid
            if not valid:
                raise ServiceError(
                    422,
                    "invalid_entry_price",
                    "A buy stop must be above ask and a sell stop must be below bid",
                )

        reference = entry or (ask if signal.direction is Direction.BUY else bid)
        minimum_distance = Decimal(symbol.trade_stops_level) * Decimal(str(symbol.point))
        if entry is not None and minimum_distance > 0:
            if signal.execution_type is ExecutionType.LIMIT:
                entry_distance = ask - entry if signal.direction is Direction.BUY else entry - bid
            else:
                entry_distance = entry - ask if signal.direction is Direction.BUY else bid - entry
            if entry_distance < minimum_distance:
                raise ServiceError(
                    422,
                    "entry_price_too_close",
                    "entry_price violates the broker's trade_stops_level",
                )

        if signal.direction is Direction.BUY:
            if signal.stop_loss is not None and signal.stop_loss >= reference:
                raise ServiceError(422, "invalid_stop_loss", "Buy stop_loss must be below entry")
            if signal.take_profit is not None and signal.take_profit <= reference:
                raise ServiceError(
                    422,
                    "invalid_take_profit",
                    "Buy take_profit must be above entry",
                )
            stop_distance = reference - signal.stop_loss if signal.stop_loss is not None else None
            profit_distance = (
                signal.take_profit - reference if signal.take_profit is not None else None
            )
        else:
            if signal.stop_loss is not None and signal.stop_loss <= reference:
                raise ServiceError(422, "invalid_stop_loss", "Sell stop_loss must be above entry")
            if signal.take_profit is not None and signal.take_profit >= reference:
                raise ServiceError(
                    422,
                    "invalid_take_profit",
                    "Sell take_profit must be below entry",
                )
            stop_distance = signal.stop_loss - reference if signal.stop_loss is not None else None
            profit_distance = (
                reference - signal.take_profit if signal.take_profit is not None else None
            )

        if stop_distance is not None and stop_distance < minimum_distance:
            raise ServiceError(422, "stop_loss_too_close", "stop_loss violates trade_stops_level")
        if profit_distance is not None and profit_distance < minimum_distance:
            raise ServiceError(
                422,
                "take_profit_too_close",
                "take_profit violates trade_stops_level",
            )

    @staticmethod
    def _validate_precision(name: str, price: Decimal, digits: int) -> None:
        quantum = Decimal(1).scaleb(-digits)
        if price != price.quantize(quantum):
            raise ServiceError(
                422,
                "invalid_price_precision",
                f"{name} has more precision than the broker allows",
                {"digits": digits},
            )

    def _build_request(
        self,
        signal: SignalRequest,
        broker_tag: str,
        symbol: SymbolSnapshot,
        tick: TickSnapshot,
    ) -> dict[str, Any]:
        constants = self.adapter.constants
        order_type = {
            (ExecutionType.MARKET, Direction.BUY): constants.order_type_buy,
            (ExecutionType.MARKET, Direction.SELL): constants.order_type_sell,
            (ExecutionType.LIMIT, Direction.BUY): constants.order_type_buy_limit,
            (ExecutionType.LIMIT, Direction.SELL): constants.order_type_sell_limit,
            (ExecutionType.STOP, Direction.BUY): constants.order_type_buy_stop,
            (ExecutionType.STOP, Direction.SELL): constants.order_type_sell_stop,
        }[(signal.execution_type, signal.direction)]

        price = signal.entry_price
        if price is None:
            price = Decimal(str(tick.ask if signal.direction is Direction.BUY else tick.bid))

        request: dict[str, Any] = {
            "action": (
                constants.trade_action_deal
                if signal.execution_type is ExecutionType.MARKET
                else constants.trade_action_pending
            ),
            "symbol": signal.symbol,
            "volume": float(signal.volume),
            "type": order_type,
            "price": float(price),
            "sl": float(signal.stop_loss or 0),
            "tp": float(signal.take_profit or 0),
            "deviation": (
                signal.deviation_points
                if signal.deviation_points is not None
                else self.settings.default_deviation_points
            ),
            "magic": self.settings.magic_number,
            "comment": broker_tag,
            "type_time": (
                constants.order_time_specified
                if signal.expires_at is not None
                else constants.order_time_gtc
            ),
            "type_filling": self._filling_policy(symbol),
        }
        if signal.expires_at is not None:
            request["expiration"] = int(signal.expires_at.timestamp())
        return request

    def _filling_policy(self, symbol: SymbolSnapshot) -> int:
        constants = self.adapter.constants
        if symbol.filling_mode & constants.symbol_filling_fok:
            return constants.order_filling_fok
        if symbol.filling_mode & constants.symbol_filling_ioc:
            return constants.order_filling_ioc
        return constants.order_filling_return

    def _normalize_result(
        self, signal: SignalRequest, result: dict[str, Any]
    ) -> SignalResponse | None:
        retcode = int(result.get("retcode", -1))
        constants = self.adapter.constants
        if retcode == constants.retcode_done:
            outcome = SignalState.FILLED
        elif retcode == constants.retcode_done_partial:
            outcome = SignalState.PARTIALLY_FILLED
        elif retcode == constants.retcode_placed:
            outcome = SignalState.PLACED
        else:
            return None
        return SignalResponse(
            signal_id=signal.signal_id,
            outcome=outcome,
            order_ticket=self._positive_int_or_none(result.get("order")),
            deal_ticket=self._positive_int_or_none(result.get("deal")),
            executed_volume=self._decimal_or_none(result.get("volume")),
            execution_price=self._decimal_or_none(result.get("price")),
            broker_retcode=retcode,
            broker_comment=str(result.get("comment", "")) or None,
            processed_at=utc_now(),
        )

    @staticmethod
    def _find_history_result(
        record: StoredSignal,
        deals: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> SignalResponse | None:
        for item, outcome in (
            *((deal, SignalState.FILLED) for deal in deals),
            *((order, SignalState.PLACED) for order in orders),
        ):
            if not str(item.get("comment", "")).startswith(record.broker_tag):
                continue
            return SignalResponse(
                signal_id=UUID(record.signal_id),
                outcome=outcome,
                order_ticket=SignalExecutionService._positive_int_or_none(
                    item.get("order", item.get("ticket") if outcome is SignalState.PLACED else None)
                ),
                deal_ticket=SignalExecutionService._positive_int_or_none(
                    item.get("ticket") if outcome is SignalState.FILLED else None
                ),
                executed_volume=SignalExecutionService._decimal_or_none(item.get("volume")),
                execution_price=SignalExecutionService._decimal_or_none(item.get("price")),
                broker_comment=str(item.get("comment", "")) or None,
                processed_at=utc_now(),
                reconciled=True,
            )
        return None

    @staticmethod
    def _status_from_record(record: StoredSignal) -> SignalStatus:
        return SignalStatus(
            signal_id=UUID(record.signal_id),
            state=record.state,
            response=(
                SignalResponse.model_validate(record.response)
                if record.response is not None
                else None
            ),
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _broker_tag(signal_id: UUID) -> str:
        digest = hashlib.sha256(str(signal_id).encode("ascii")).hexdigest()
        return f"sig:{digest[:27]}"

    @staticmethod
    def _stored_error(error: ServiceError) -> dict[str, Any]:
        return {"status_code": error.status_code, **error.as_dict()}

    @staticmethod
    def _broker_details(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "retcode": result.get("retcode"),
            "comment": result.get("comment"),
            "retcode_external": result.get("retcode_external"),
        }

    def _safe_last_error(self) -> Any:
        try:
            return self.adapter.last_error()
        except Exception:
            return None

    @staticmethod
    def _positive_int_or_none(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
