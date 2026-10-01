"""cTrader execution over the multi-account protobuf gateway.

Event-driven: ``dispatch`` sends one request and maps the first response
event; every later event for the same client order ID (a fill after an
accept, a cancel of a pending order) settles the target through the ledger
the service attached. Reconnects replay open orders, which re-attach to the
ledger by client order ID too.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from google.protobuf.message import Message
from ta_contracts import (
    BrokerOrder,
    BrokerPosition,
    Direction,
    ExecutionType,
    OperationAction,
    OrderRequest,
    SymbolInfo,
    TargetState,
    TimeInForce,
)
from ta_core import ServiceError
from ta_plugin_api.execution import LedgerPort, TargetOutcome

from .errors import CTraderError
from .gateway import CTraderGateway, protobuf_dict
from .proto import (
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

__all__ = ["CTraderExecution"]


class CTraderExecution:
    name = "ctrader"

    def __init__(self, settings: Any, gateway: CTraderGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self._ledger: LedgerPort | None = None

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        await self.gateway.start()

    async def wait_ready(self, timeout_seconds: float) -> bool:
        return await self.gateway.wait_ready(timeout_seconds=timeout_seconds)

    async def close(self) -> None:
        await self.gateway.close()

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        return self.gateway.readiness()

    def attach_ledger(self, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self.gateway.on_execution_event = self._on_execution_event
        self.gateway.on_reconciled = self._on_reconciled

    async def reconcile(self) -> None:
        """Nothing to do eagerly: the gateway reconciles every account on each
        (re)connect and calls back into ``_on_reconciled``."""

    async def reconcile_unknown(self) -> None:
        """Nothing to sweep: execution events settle targets as they arrive."""

    # --- accounts -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return tuple(self.gateway.aliases())

    def has_live_accounts(self) -> bool:
        return any(
            self.gateway.account(alias).definition.environment == "live"
            for alias in self.gateway.aliases()
        )

    def account_statuses(self) -> list[dict[str, Any]]:
        return [{"provider": self.name, **status} for status in self.gateway.account_statuses()]

    def orders(self, account: str) -> list[BrokerOrder]:
        return self.gateway.list_orders(account)

    def positions(self, account: str) -> list[BrokerPosition]:
        return self.gateway.list_positions(account)

    def _account_for_execution(self, alias: str, *, closing: bool = False) -> Any:
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

    # --- operations -----------------------------------------------------------

    def client_order_id(self, operation_id: UUID, account: str) -> str:
        account_hash = hashlib.sha256(account.encode()).hexdigest()[:12]
        return f"{operation_id.hex}-{account_hash}"[:50]

    async def prepare(
        self,
        action: OperationAction,
        request: Any,
        target: Any,
        client_order_id: str,
    ) -> Message:
        if action is OperationAction.PLACE_ORDER:
            return self._prepare_place(request, target, client_order_id)
        if action is OperationAction.CANCEL_ORDER:
            return self._prepare_cancel(target)
        if action is OperationAction.AMEND_ORDER:
            return self._prepare_amend_order(target)
        if action is OperationAction.AMEND_POSITION:
            return self._prepare_amend_position(target)
        if action is OperationAction.CLOSE_POSITION:
            return self._prepare_close(target)
        raise ServiceError(501, "action_not_supported", f"cTrader does not support {action.value}")

    def _prepare_place(self, request: OrderRequest, target: Any, client_order_id: str) -> Message:
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
        return self._new_order_message(
            request, account.definition.ctid_trader_account_id, symbol, volume, client_order_id
        )

    def _prepare_cancel(self, target: Any) -> Message:
        account = self._account_for_execution(target.account)
        if target.order_id not in account.orders:
            raise ServiceError(
                422,
                "order_not_found",
                "Pending order is not present in reconciled account state",
                {"account": target.account, "order_id": target.order_id},
            )
        return ProtoOACancelOrderReq(
            ctidTraderAccountId=account.definition.ctid_trader_account_id,
            orderId=target.order_id,
        )

    def _prepare_amend_order(self, target: Any) -> Message:
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
        return ProtoOAAmendOrderReq(**kwargs)

    def _prepare_amend_position(self, target: Any) -> Message:
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
        return ProtoOAAmendPositionSLTPReq(**kwargs)

    def _prepare_close(self, target: Any) -> Message:
        account = self._account_for_execution(target.account, closing=True)
        position = account.positions.get(target.position_id)
        if position is None:
            raise ServiceError(422, "position_not_found", "Position was not reconciled")
        assert account.catalog is not None
        symbol = account.catalog.info(account.catalog.name_for_id(int(position.tradeData.symbolId)))
        volume = self._volume_to_protocol(target.volume_lots, symbol)
        if volume > int(position.tradeData.volume):
            raise ServiceError(
                422, "close_volume_too_large", "Close volume exceeds the open position"
            )
        return ProtoOAClosePositionReq(
            ctidTraderAccountId=account.definition.ctid_trader_account_id,
            positionId=target.position_id,
            volume=volume,
        )

    async def dispatch(
        self,
        operation_id: UUID,
        account: str,
        action: OperationAction,
        prepared: Message,
        client_order_id: str,
    ) -> TargetOutcome:
        try:
            response = await self.gateway.request(account, prepared, correlation_id=client_order_id)
        except CTraderError as exc:
            return TargetOutcome(
                TargetState.REJECTED,
                {"error_code": exc.error_code, "error_message": str(exc)},
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            return TargetOutcome(
                TargetState.UNKNOWN,
                {"error_code": type(exc).__name__, "error_message": str(exc)},
            )
        if isinstance(response, ProtoOAExecutionEvent | ProtoOAOrderErrorEvent):
            self._record_event(operation_id, account, response)
            return self._outcome(account, action, response)
        return TargetOutcome(
            TargetState.UNKNOWN,
            {"error_code": "UNEXPECTED_RESPONSE", "error_message": type(response).__name__},
        )

    # --- asynchronous settlement ----------------------------------------------

    def _record_event(
        self,
        operation_id: UUID | str | None,
        account: str,
        event: Message,
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.append_event(
            operation_id=operation_id,
            account=account,
            event_type=type(event).__name__,
            payload=protobuf_dict(event),
        )

    def _on_execution_event(
        self,
        account: str,
        event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent,
        correlation_id: str | None,
    ) -> None:
        ledger = self._ledger
        if ledger is None:
            return
        client_order_id = correlation_id
        if (
            not client_order_id
            and isinstance(event, ProtoOAExecutionEvent)
            and event.HasField("order")
            and event.order.HasField("clientOrderId")
        ):
            client_order_id = str(event.order.clientOrderId)
        match = ledger.find_by_client_order_id(client_order_id) if client_order_id else None
        if match is None:
            self._record_event(None, account, event)
            return
        operation_id, matched_account = match
        operation = ledger.get(operation_id)
        if operation is None:
            return
        self._record_event(operation_id, matched_account, event)
        outcome = self._outcome(matched_account, operation.action, event)
        ledger.update_target(operation_id, matched_account, outcome.state, **outcome.values)

    def _on_reconciled(self, account_alias: str) -> None:
        ledger = self._ledger
        if ledger is None:
            return
        account = self.gateway.account(account_alias)
        for order in account.orders.values():
            if not order.HasField("clientOrderId"):
                continue
            match = ledger.find_by_client_order_id(str(order.clientOrderId))
            if match is None or match[1] != account_alias:
                continue
            operation_id, _ = match
            ledger.update_target(
                operation_id,
                account_alias,
                TargetState.PLACED,
                order_id=int(order.orderId),
                position_id=int(order.positionId) if order.HasField("positionId") else None,
            )

    def _outcome(
        self,
        account: str,
        action: OperationAction,
        event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent,
    ) -> TargetOutcome:
        if isinstance(event, ProtoOAOrderErrorEvent):
            return TargetOutcome(
                TargetState.REJECTED,
                {
                    "order_id": int(event.orderId) if event.HasField("orderId") else None,
                    "position_id": int(event.positionId) if event.HasField("positionId") else None,
                    "error_code": str(event.errorCode),
                    "error_message": (
                        str(event.description) if event.HasField("description") else None
                    ),
                },
            )

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
        return TargetOutcome(state, values)

    # --- request building -----------------------------------------------------

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

        account = self.gateway._by_id[account_id]
        reference: Decimal | None = request.entry_price
        if request.execution_type is ExecutionType.MARKET:
            quote = account.hub.last_quote(request.instrument)
            if quote is not None:
                price = quote.ask if request.direction is Direction.BUY else quote.bid
                if price is not None:
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
    def _require_aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ServiceError(422, "naive_timestamp", f"{field} must include a timezone")
