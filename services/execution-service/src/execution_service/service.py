from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from google.protobuf.message import Message
from pydantic import BaseModel
from ta_contracts import (
    AmendOrderRequest,
    CancelOrderRequest,
    ClosePositionRequest,
    Direction,
    ExecutionType,
    OperationAction,
    OperationResponse,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
    SymbolInfo,
    TargetState,
    TimeInForce,
)
from ta_store import ExecutionRepository, OperationConflictError

from .config import Settings
from .ctrader.gateway import CTraderGateway, protobuf_dict
from .ctrader.proto import (
    ProtoOAAmendOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOACancelOrderReq,
    ProtoOAClosePositionReq,
    ProtoOAExecutionEvent,
    ProtoOAExecutionType,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
    ProtoOAOrderType,
    ProtoOATimeInForce,
    ProtoOATradeSide,
)
from .errors import CTraderError, ServiceError
from .logging_config import log_event


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        gateway: CTraderGateway,
        repository: ExecutionRepository,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.repository = repository
        self.gateway.on_execution_event = self._on_execution_event
        self.gateway.on_reconciled = self._on_reconciled

    async def place_order(self, request: OrderRequest) -> OperationResponse:
        self._validate_common(request)
        prepared: list[tuple[str, str, Message]] = []
        for target in request.targets:
            account = self._account_for_execution(target.account)
            catalog = account.catalog
            assert catalog is not None
            try:
                symbol = catalog.info(request.instrument)
            except Exception as exc:
                raise ServiceError(
                    422,
                    "instrument_not_available",
                    "Canonical instrument is not configured for a target account",
                    {"account": target.account, "instrument": request.instrument},
                ) from exc
            if not symbol.enabled or symbol.trading_mode not in {None, 0}:
                raise ServiceError(
                    422,
                    "instrument_not_tradable",
                    "The broker has not enabled opening trades for this instrument",
                    {"account": target.account, "instrument": request.instrument},
                )
            volume = self._volume_to_protocol(target.volume_lots, symbol)
            client_order_id = self._client_order_id(request.operation_id, target.account)
            message = self._new_order_message(
                request, account.definition.ctid_trader_account_id, symbol, volume, client_order_id
            )
            prepared.append((target.account, client_order_id, message))
        return await self._dispatch(
            request,
            OperationAction.PLACE_ORDER,
            prepared,
        )

    async def cancel_order(self, request: CancelOrderRequest) -> OperationResponse:
        self._validate_common(request)
        prepared = []
        for target in request.targets:
            account = self._account_for_execution(target.account)
            if target.order_id not in account.orders:
                raise ServiceError(
                    422,
                    "order_not_found",
                    "Pending order is not present in reconciled account state",
                    {"account": target.account, "order_id": target.order_id},
                )
            correlation_id = self._client_order_id(request.operation_id, target.account)
            prepared.append(
                (
                    target.account,
                    correlation_id,
                    ProtoOACancelOrderReq(
                        ctidTraderAccountId=account.definition.ctid_trader_account_id,
                        orderId=target.order_id,
                    ),
                )
            )
        return await self._dispatch(request, OperationAction.CANCEL_ORDER, prepared)

    async def amend_order(self, request: AmendOrderRequest) -> OperationResponse:
        self._validate_common(request)
        prepared = []
        for target in request.targets:
            account = self._account_for_execution(target.account)
            order = account.orders.get(target.order_id)
            if order is None:
                raise ServiceError(422, "order_not_found", "Pending order was not reconciled")
            kwargs: dict[str, Any] = {
                "ctidTraderAccountId": account.definition.ctid_trader_account_id,
                "orderId": target.order_id,
            }
            if target.volume_lots is not None:
                assert account.catalog is not None
                symbol = account.catalog.info(
                    account.catalog.name_for_id(int(order.tradeData.symbolId))
                )
                kwargs["volume"] = self._volume_to_protocol(target.volume_lots, symbol)
            if target.entry_price is not None:
                order_type = ProtoOAOrderType.Name(int(order.orderType))
                field = "limitPrice" if order_type == "LIMIT" else "stopPrice"
                kwargs[field] = float(target.entry_price)
            if target.stop_loss is not None:
                kwargs["stopLoss"] = float(target.stop_loss)
            if target.take_profit is not None:
                kwargs["takeProfit"] = float(target.take_profit)
            if target.expires_at is not None:
                self._require_aware(target.expires_at, "expires_at")
                kwargs["expirationTimestamp"] = int(target.expires_at.timestamp() * 1000)
            if len(kwargs) == 2:
                raise ServiceError(422, "empty_amendment", "At least one order field is required")
            correlation_id = self._client_order_id(request.operation_id, target.account)
            prepared.append((target.account, correlation_id, ProtoOAAmendOrderReq(**kwargs)))
        return await self._dispatch(request, OperationAction.AMEND_ORDER, prepared)

    async def amend_position(self, request: PositionProtectionRequest) -> OperationResponse:
        self._validate_common(request)
        prepared = []
        for target in request.targets:
            account = self._account_for_execution(target.account)
            if target.position_id not in account.positions:
                raise ServiceError(422, "position_not_found", "Position was not reconciled")
            if target.stop_loss is None and target.take_profit is None:
                raise ServiceError(422, "empty_amendment", "stop_loss or take_profit is required")
            kwargs: dict[str, Any] = {
                "ctidTraderAccountId": account.definition.ctid_trader_account_id,
                "positionId": target.position_id,
                "trailingStopLoss": target.trailing_stop_loss,
            }
            if target.stop_loss is not None:
                kwargs["stopLoss"] = float(target.stop_loss)
            if target.take_profit is not None:
                kwargs["takeProfit"] = float(target.take_profit)
            if account.trader is not None and bool(account.trader.isLimitedRisk):
                kwargs["guaranteedStopLoss"] = True
            correlation_id = self._client_order_id(request.operation_id, target.account)
            prepared.append((target.account, correlation_id, ProtoOAAmendPositionSLTPReq(**kwargs)))
        return await self._dispatch(request, OperationAction.AMEND_POSITION, prepared)

    async def close_position(self, request: ClosePositionRequest) -> OperationResponse:
        self._validate_common(request)
        prepared = []
        for target in request.targets:
            account = self._account_for_execution(target.account, closing=True)
            position = account.positions.get(target.position_id)
            if position is None:
                raise ServiceError(422, "position_not_found", "Position was not reconciled")
            assert account.catalog is not None
            symbol = account.catalog.info(
                account.catalog.name_for_id(int(position.tradeData.symbolId))
            )
            volume = self._volume_to_protocol(target.volume_lots, symbol)
            if volume > int(position.tradeData.volume):
                raise ServiceError(
                    422, "close_volume_too_large", "Close volume exceeds the open position"
                )
            correlation_id = self._client_order_id(request.operation_id, target.account)
            prepared.append(
                (
                    target.account,
                    correlation_id,
                    ProtoOAClosePositionReq(
                        ctidTraderAccountId=account.definition.ctid_trader_account_id,
                        positionId=target.position_id,
                        volume=volume,
                    ),
                )
            )
        return await self._dispatch(request, OperationAction.CLOSE_POSITION, prepared)

    def status(self, operation_id: UUID) -> OperationResponse:
        response = self.repository.get(operation_id)
        if response is None:
            raise ServiceError(404, "operation_not_found", "No operation has this ID")
        return response

    async def _dispatch(
        self,
        request: BaseModel,
        action: OperationAction,
        prepared: list[tuple[str, str | None, Message]],
    ) -> OperationResponse:
        payload_json = request.model_dump_json(exclude_none=False)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        operation_id = request.operation_id
        try:
            existing, created = self.repository.reserve(
                operation_id=operation_id,
                action=action,
                source=request.source,
                payload_hash=payload_hash,
                payload_json=payload_json,
                targets=[(account, client_order_id) for account, client_order_id, _ in prepared],
            )
        except OperationConflictError as exc:
            raise ServiceError(409, "operation_id_conflict", str(exc)) from exc
        if not created:
            return existing

        correlations = {account: correlation for account, correlation, _ in prepared}

        async def send(account: str, message: Message) -> None:
            self.repository.update_target(operation_id, account, TargetState.DISPATCHED)
            try:
                response = await self.gateway.request(
                    account, message, correlation_id=correlations[account]
                )
                if isinstance(response, ProtoOAExecutionEvent | ProtoOAOrderErrorEvent):
                    self._apply_event(operation_id, account, action, response)
                else:
                    self.repository.update_target(
                        operation_id,
                        account,
                        TargetState.UNKNOWN,
                        error_code="UNEXPECTED_RESPONSE",
                        error_message=type(response).__name__,
                    )
            except CTraderError as exc:
                self.repository.update_target(
                    operation_id,
                    account,
                    TargetState.REJECTED,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )
            except (TimeoutError, ConnectionError, OSError) as exc:
                self.repository.update_target(
                    operation_id,
                    account,
                    TargetState.UNKNOWN,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )

        tasks = [asyncio.create_task(send(account, message)) for account, _, message in prepared]
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
                    if target.state in {
                        TargetState.RESERVED,
                        TargetState.DISPATCHED,
                        TargetState.ACCEPTED,
                        TargetState.PARTIALLY_FILLED,
                    }:
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

    def _on_execution_event(
        self,
        account: str,
        event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent,
        correlation_id: str | None,
    ) -> None:
        client_order_id = correlation_id
        if (
            not client_order_id
            and isinstance(event, ProtoOAExecutionEvent)
            and event.HasField("order")
        ):
            if event.order.HasField("clientOrderId"):
                client_order_id = str(event.order.clientOrderId)
        if not client_order_id:
            self.repository.append_event(
                account=account,
                event_type=type(event).__name__,
                payload=protobuf_dict(event),
            )
            return
        match = self.repository.find_by_client_order_id(client_order_id)
        if match is None:
            self.repository.append_event(
                account=account,
                event_type=type(event).__name__,
                payload=protobuf_dict(event),
            )
            return
        operation_id, matched_account = match
        operation = self.repository.get(operation_id)
        if operation is None:
            return
        self._apply_event(operation_id, matched_account, operation.action, event)

    def _on_reconciled(self, account_alias: str) -> None:
        account = self.gateway.account(account_alias)
        for order in account.orders.values():
            if not order.HasField("clientOrderId"):
                continue
            match = self.repository.find_by_client_order_id(str(order.clientOrderId))
            if match is None or match[1] != account_alias:
                continue
            operation_id, _ = match
            self.repository.update_target(
                operation_id,
                account_alias,
                TargetState.PLACED,
                order_id=int(order.orderId),
                position_id=int(order.positionId) if order.HasField("positionId") else None,
            )

    def _apply_event(
        self,
        operation_id: UUID | str,
        account: str,
        action: OperationAction,
        event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent,
    ) -> None:
        payload = protobuf_dict(event)
        self.repository.append_event(
            operation_id=operation_id,
            account=account,
            event_type=type(event).__name__,
            payload=payload,
        )
        if isinstance(event, ProtoOAOrderErrorEvent):
            self.repository.update_target(
                operation_id,
                account,
                TargetState.REJECTED,
                order_id=int(event.orderId) if event.HasField("orderId") else None,
                position_id=int(event.positionId) if event.HasField("positionId") else None,
                error_code=str(event.errorCode),
                error_message=str(event.description) if event.HasField("description") else None,
            )
            return

        execution_name = ProtoOAExecutionType.Name(int(event.executionType))
        if execution_name in {"ORDER_REJECTED", "ORDER_CANCEL_REJECTED"}:
            state = TargetState.REJECTED
        elif execution_name == "ORDER_CANCELLED":
            state = TargetState.CANCELLED
        elif execution_name == "ORDER_REPLACED":
            state = TargetState.AMENDED
        elif execution_name == "ORDER_PARTIAL_FILL":
            state = TargetState.PARTIALLY_FILLED
        elif execution_name == "ORDER_FILLED":
            state = (
                TargetState.CLOSED
                if action is OperationAction.CLOSE_POSITION
                else TargetState.FILLED
            )
        elif execution_name == "ORDER_ACCEPTED":
            is_market = (
                event.HasField("order")
                and ProtoOAOrderType.Name(int(event.order.orderType)) == "MARKET"
            )
            state = TargetState.ACCEPTED if is_market else TargetState.PLACED
        else:
            state = TargetState.UNKNOWN

        values: dict[str, Any] = {
            "error_code": str(event.errorCode) if event.HasField("errorCode") else None
        }
        if event.HasField("order"):
            values["order_id"] = int(event.order.orderId)
            if event.order.HasField("positionId"):
                values["position_id"] = int(event.order.positionId)
            if event.order.HasField("executionPrice"):
                values["execution_price"] = Decimal(str(event.order.executionPrice))
        if event.HasField("position"):
            values["position_id"] = int(event.position.positionId)
        if event.HasField("deal"):
            values["deal_id"] = int(event.deal.dealId)
            if event.deal.HasField("executionPrice"):
                values["execution_price"] = Decimal(str(event.deal.executionPrice))
            if event.deal.HasField("filledVolume"):
                target_account = self.gateway.account(account)
                assert target_account.catalog is not None
                info = target_account.catalog.info(
                    target_account.catalog.name_for_id(int(event.deal.symbolId))
                )
                if info.lot_size:
                    values["executed_volume_lots"] = Decimal(event.deal.filledVolume) / Decimal(
                        info.lot_size
                    )
        self.repository.update_target(operation_id, account, state, **values)

    def _validate_common(self, request: BaseModel) -> None:
        if request.source.strip().lower() not in self.settings.allowed_order_sources:
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

    def _account_for_execution(self, alias: str, *, closing: bool = False):
        try:
            account = self.gateway.account(alias)
        except KeyError as exc:
            raise ServiceError(422, "account_not_allowed", str(exc)) from exc
        if not self.gateway.account_ready(alias):
            raise ServiceError(503, "account_not_ready", f"Account {alias} is not ready")
        if account.definition.environment == "live" and not self.settings.live_trading_enabled:
            raise ServiceError(
                503,
                "live_trading_disabled",
                "LIVE_TRADING_ENABLED is required for live accounts",
            )
        if account.trader is not None:
            access_rights = int(account.trader.accessRights)
            allowed = access_rights == 0 or (closing and access_rights == 1)
            if not allowed:
                raise ServiceError(
                    503,
                    "account_trading_not_allowed",
                    "The account access rights do not allow this operation",
                )
        return account

    def _volume_to_protocol(self, lots: Decimal, symbol: SymbolInfo) -> int:
        maximum = self.settings.max_volume_lots
        assert maximum is not None
        if lots > maximum:
            raise ServiceError(
                422,
                "volume_exceeds_limit",
                "Target volume exceeds MAX_VOLUME_LOTS",
                {"maximum": str(maximum)},
            )
        if not symbol.lot_size:
            raise ServiceError(503, "symbol_metadata_incomplete", "Symbol has no lotSize")
        raw = lots * Decimal(symbol.lot_size)
        if raw != raw.to_integral_value():
            raise ServiceError(422, "invalid_volume_step", "Volume cannot be represented exactly")
        volume = int(raw)
        if symbol.min_volume is not None and volume < symbol.min_volume:
            raise ServiceError(422, "volume_below_minimum", "Volume is below broker minimum")
        if symbol.max_volume is not None and volume > symbol.max_volume:
            raise ServiceError(422, "volume_above_maximum", "Volume is above broker maximum")
        if symbol.step_volume and symbol.min_volume is not None:
            if (volume - symbol.min_volume) % symbol.step_volume:
                raise ServiceError(422, "invalid_volume_step", "Volume violates broker step")
        return volume

    def _new_order_message(
        self,
        request: OrderRequest,
        account_id: int,
        symbol: SymbolInfo,
        volume: int,
        client_order_id: str,
    ) -> ProtoOANewOrderReq:
        kwargs: dict[str, Any] = {
            "ctidTraderAccountId": account_id,
            "symbolId": symbol.symbol_id,
            "orderType": ProtoOAOrderType.Value(request.execution_type.name),
            "tradeSide": ProtoOATradeSide.Value(request.direction.name),
            "volume": volume,
            "clientOrderId": client_order_id,
            "label": request.source[:100],
            "comment": request.note or request.source,
        }
        if request.execution_type is not ExecutionType.MARKET:
            kwargs["timeInForce"] = ProtoOATimeInForce.Value(
                "GOOD_TILL_DATE" if request.time_in_force is TimeInForce.GTD else "GOOD_TILL_CANCEL"
            )
        if request.execution_type is ExecutionType.LIMIT:
            kwargs["limitPrice"] = float(request.entry_price)
        elif request.execution_type is ExecutionType.STOP:
            kwargs["stopPrice"] = float(request.entry_price)
        if request.expires_at is not None:
            kwargs["expirationTimestamp"] = int(request.expires_at.timestamp() * 1000)

        reference: Decimal | None = request.entry_price
        if request.execution_type is ExecutionType.MARKET:
            account = self.gateway._by_id[account_id]
            tick = account.hub.last_tick(request.instrument)
            if tick is not None:
                price = tick.ask if request.direction is Direction.BUY else tick.bid
                reference = Decimal(str(price))
        stop_distance = self._protection_distance(
            request.direction, reference, request.stop_loss, request.stop_loss_distance, stop=True
        )
        take_distance = self._protection_distance(
            request.direction,
            reference,
            request.take_profit,
            request.take_profit_distance,
            stop=False,
        )
        if request.execution_type is ExecutionType.MARKET:
            if stop_distance is not None:
                kwargs["relativeStopLoss"] = int(stop_distance * Decimal(100000))
            if take_distance is not None:
                kwargs["relativeTakeProfit"] = int(take_distance * Decimal(100000))
        else:
            if request.stop_loss is not None:
                kwargs["stopLoss"] = float(request.stop_loss)
            elif stop_distance is not None and reference is not None:
                kwargs["stopLoss"] = float(
                    reference - stop_distance
                    if request.direction is Direction.BUY
                    else reference + stop_distance
                )
            if request.take_profit is not None:
                kwargs["takeProfit"] = float(request.take_profit)
            elif take_distance is not None and reference is not None:
                kwargs["takeProfit"] = float(
                    reference + take_distance
                    if request.direction is Direction.BUY
                    else reference - take_distance
                )
        account = self.gateway._by_id[account_id]
        if account.trader is not None and bool(account.trader.isLimitedRisk):
            if stop_distance is None:
                raise ServiceError(
                    422, "guaranteed_stop_required", "Limited-risk account requires stop loss"
                )
            kwargs["guaranteedStopLoss"] = True
        return ProtoOANewOrderReq(**kwargs)

    @staticmethod
    def _protection_distance(
        direction: Direction,
        reference: Decimal | None,
        absolute: Decimal | None,
        distance: Decimal | None,
        *,
        stop: bool,
    ) -> Decimal | None:
        if distance is not None:
            return distance
        if absolute is None:
            return None
        if reference is None:
            raise ServiceError(
                503,
                "tick_unavailable",
                "A current quote is required for absolute market protection",
            )
        expected_below = (direction is Direction.BUY) == stop
        calculated = reference - absolute if expected_below else absolute - reference
        if calculated <= 0:
            leg = "stop_loss" if stop else "take_profit"
            raise ServiceError(422, f"invalid_{leg}", f"{leg} is on the wrong side of entry")
        return calculated

    @staticmethod
    def _client_order_id(operation_id: UUID, account: str) -> str:
        account_hash = hashlib.sha256(account.encode()).hexdigest()[:12]
        return f"{operation_id.hex}-{account_hash}"[:50]

    @staticmethod
    def _require_aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ServiceError(422, "naive_timestamp", f"{field} must include a timezone")
