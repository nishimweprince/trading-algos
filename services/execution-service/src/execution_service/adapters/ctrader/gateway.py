from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from ta_contracts import BrokerOrder, BrokerPosition, Candle, Direction, SymbolInfo, Timeframe

from ...config import CTRADER_HOSTS, AccountDefinition, Settings
from ...errors import CTraderError, SymbolResolutionError
from ...hub import MarketDataHub
from ...logging_config import log_event
from .decode import decode_spot, decode_trendbars, period_duration
from .proto import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAApplicationAuthReq,
    ProtoOAClientDisconnectEvent,
    ProtoOAExecutionEvent,
    ProtoOAExecutionType,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetTrendbarsReq,
    ProtoOAOrderErrorEvent,
    ProtoOAOrderStatus,
    ProtoOAPositionStatus,
    ProtoOAReconcileReq,
    ProtoOARefreshTokenReq,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
    ProtoOATrendbarPeriod,
)
from .protocol import CTraderProtocolClient, tls_connector
from .symbols import SymbolCatalog
from .tokens import TokenPair, TokenStore

ExecutionHandler = Callable[[str, ProtoOAExecutionEvent | ProtoOAOrderErrorEvent, str | None], None]
ReconcileHandler = Callable[[str], None]


@dataclass
class GatewayAccount:
    definition: AccountDefinition
    hub: MarketDataHub
    catalog: SymbolCatalog | None = None
    trader: Message | None = None
    positions: dict[int, Message] = field(default_factory=dict)
    orders: dict[int, Message] = field(default_factory=dict)
    reconciled: bool = False


class CTraderGateway:
    """One token owner and at most one broker connection per environment."""

    def __init__(self, settings: Settings) -> None:
        if not settings.gateway_enabled:
            raise ValueError("CTraderGateway requires ACCOUNTS_CONFIG_PATH")
        self.settings = settings
        self._tokens = TokenStore(
            settings.token_cache_path,
            fallback=TokenPair(
                access_token=settings.access_token.get_secret_value(),
                refresh_token=(
                    settings.refresh_token.get_secret_value() if settings.refresh_token else None
                ),
            ),
        )
        self._accounts = {
            account.alias: GatewayAccount(
                definition=account,
                hub=MarketDataHub(queue_size=settings.subscriber_queue_size),
            )
            for account in settings.enabled_accounts
        }
        self._by_id = {
            account.definition.ctid_trader_account_id: account
            for account in self._accounts.values()
        }
        self._clients: dict[str, CTraderProtocolClient] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._request_workers: dict[str, asyncio.Task[None]] = {}
        self._token_refresher: asyncio.Task[None] | None = None
        self._environment_ready: dict[str, asyncio.Event] = {
            environment: asyncio.Event()
            for environment in {a.definition.environment for a in self._accounts.values()}
        }
        self._reconnects: dict[str, int] = {
            environment: 0 for environment in self._environment_ready
        }
        self._closing = False
        self._refresh_lock = asyncio.Lock()
        self._request_locks = {
            environment: asyncio.Lock() for environment in self._environment_ready
        }
        self._last_request = {environment: 0.0 for environment in self._environment_ready}
        self._historical_locks = {
            environment: asyncio.Lock() for environment in self._environment_ready
        }
        self._last_historical = {environment: 0.0 for environment in self._environment_ready}
        self._request_queues: dict[
            str,
            asyncio.PriorityQueue[
                tuple[int, int, bool, Message, str | None, asyncio.Future[Message]]
            ],
        ] = {environment: asyncio.PriorityQueue() for environment in self._environment_ready}
        self._request_sequence = count()
        self.on_execution_event: ExecutionHandler | None = None
        self.on_reconciled: ReconcileHandler | None = None

    async def start(self) -> None:
        self._tokens.load()
        self._closing = False
        for environment in self._environment_ready:
            self._tasks[environment] = asyncio.create_task(
                self._supervise(environment), name=f"ctrader-{environment}-supervisor"
            )
            self._request_workers[environment] = asyncio.create_task(
                self._request_worker(environment), name=f"ctrader-{environment}-requests"
            )
        self._token_refresher = asyncio.create_task(
            self._refresh_loop(), name="ctrader-gateway-token-refresh"
        )

    async def close(self) -> None:
        self._closing = True
        if self._token_refresher is not None:
            self._token_refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._token_refresher
            self._token_refresher = None
        for task in self._tasks.values():
            task.cancel()
        for task in self._request_workers.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for task in self._request_workers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._request_workers.clear()
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        for ready in self._environment_ready.values():
            ready.clear()

    async def wait_ready(self, timeout_seconds: float) -> bool:
        async def wait_all() -> None:
            await asyncio.gather(*(event.wait() for event in self._environment_ready.values()))

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_seconds):
                await wait_all()
        return self.is_ready

    @property
    def is_ready(self) -> bool:
        return bool(self._environment_ready) and all(
            event.is_set() for event in self._environment_ready.values()
        )

    def account_ready(self, alias: str) -> bool:
        account = self.account(alias)
        return (
            self._environment_ready[account.definition.environment].is_set()
            and account.catalog is not None
            and account.reconciled
        )

    def account(self, alias: str) -> GatewayAccount:
        try:
            return self._accounts[alias]
        except KeyError as exc:
            raise KeyError(f"unknown or disabled account alias {alias!r}") from exc

    @property
    def default_account_alias(self) -> str:
        assert self.settings.default_market_data_account is not None
        return self.settings.default_market_data_account

    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self._accounts))

    async def _supervise(self, environment: str) -> None:
        backoff = self.settings.reconnect_initial_backoff_seconds
        first = True
        while not self._closing:
            error: str | None = None
            try:
                if not first:
                    self._reconnects[environment] += 1
                first = False
                await self._connect_environment(environment)
                self._environment_ready[environment].set()
                log_event("ctrader_environment_connected", environment=environment)
                closed = await self._clients[environment].wait_closed()
                if closed is not None:
                    error = f"{type(closed).__name__}: {closed}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log_event(
                    "ctrader_environment_connect_failed",
                    level=logging.ERROR,
                    environment=environment,
                    reason=type(exc).__name__,
                    detail=str(exc),
                )
            finally:
                self._environment_ready[environment].clear()
                for account in self._environment_accounts(environment):
                    account.reconciled = False
                    account.hub.publish_status("reconnecting", error=error)
                client = self._clients.pop(environment, None)
                if client is not None:
                    await client.close()
            if self._closing:
                return
            delay = min(
                backoff * random.uniform(0.5, 1.5),  # noqa: S311
                self.settings.reconnect_max_backoff_seconds,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self.settings.reconnect_max_backoff_seconds)

    async def _connect_environment(self, environment: str) -> None:
        client = CTraderProtocolClient(
            tls_connector(CTRADER_HOSTS[environment], self.settings.ctrader_port),
            on_event=lambda message: self._on_event(environment, message, None),
            on_envelope_event=lambda message, mid: self._on_event(environment, message, mid),
            request_timeout=self.settings.request_timeout_seconds,
            heartbeat_interval=self.settings.heartbeat_interval_seconds,
            connect_timeout=self.settings.connect_timeout_seconds,
        )
        self._clients[environment] = client
        await client.connect()
        await client.request(
            ProtoOAApplicationAuthReq(
                clientId=self.settings.client_id.get_secret_value(),
                clientSecret=self.settings.client_secret.get_secret_value(),
            )
        )
        await self._authenticate_and_load(environment, client)

    async def _authenticate_and_load(self, environment: str, client: CTraderProtocolClient) -> None:
        if self._tokens.is_invalidated:
            await self._refresh_token(client)
        discovered = await client.request(
            ProtoOAGetAccountListByAccessTokenReq(accessToken=self._tokens.current.access_token)
        )
        accessible = {
            int(item.ctidTraderAccountId): bool(item.isLive)
            for item in discovered.ctidTraderAccount
        }
        accounts = self._environment_accounts(environment)
        for account in accounts:
            account_id = account.definition.ctid_trader_account_id
            expected_live = environment == "live"
            if account_id not in accessible:
                raise CTraderError("ACCOUNT_NOT_AUTHORIZED", f"account {account.definition.alias}")
            if accessible[account_id] != expected_live:
                raise CTraderError(
                    "ACCOUNT_ENVIRONMENT_MISMATCH",
                    f"account {account.definition.alias} does not belong to {environment}",
                )

        for account in accounts:
            account_id = account.definition.ctid_trader_account_id
            try:
                await client.request(
                    ProtoOAAccountAuthReq(
                        ctidTraderAccountId=account_id,
                        accessToken=self._tokens.current.access_token,
                    )
                )
            except CTraderError as exc:
                if exc.error_code not in {
                    "CH_ACCESS_TOKEN_INVALID",
                    "OA_AUTH_TOKEN_EXPIRED",
                    "CH_ACCESS_TOKEN_EXPIRED",
                }:
                    raise
                await self._refresh_token(client)
                await client.request(
                    ProtoOAAccountAuthReq(
                        ctidTraderAccountId=account_id,
                        accessToken=self._tokens.current.access_token,
                    )
                )
            trader_response = await client.request(ProtoOATraderReq(ctidTraderAccountId=account_id))
            account.trader = trader_response.trader
            account.catalog = await self._load_catalog(client, account)
            await self._reconcile_account(client, account)
            await client.request(
                ProtoOASubscribeSpotsReq(
                    ctidTraderAccountId=account_id,
                    symbolId=account.catalog.ids(),
                    subscribeToSpotTimestamp=True,
                )
            )
            account.hub.publish_status("connected")

    async def _refresh_token(self, client: CTraderProtocolClient) -> None:
        async with self._refresh_lock:
            refresh_token = self._tokens.current.refresh_token
            if not refresh_token:
                raise CTraderError("NO_REFRESH_TOKEN", "no refresh token configured")
            response = await client.request(ProtoOARefreshTokenReq(refreshToken=refresh_token))
            self._tokens.record_refresh(
                access_token=response.accessToken,
                refresh_token=response.refreshToken,
                expires_in=response.expiresIn,
            )
            for environment, other in tuple(self._clients.items()):
                if other is not client:
                    log_event("ctrader_reauth_required", environment=environment)
                    await other.close()

    async def _refresh_loop(self) -> None:
        while True:
            delay = self._tokens.current.seconds_until_refresh()
            if delay is None:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(delay)
            clients = [client for client in self._clients.values() if client.is_connected]
            if not clients:
                await asyncio.sleep(1)
                continue
            try:
                await self._refresh_token(clients[0])
                for client in tuple(self._clients.values()):
                    await client.close()
            except Exception as exc:
                log_event(
                    "proactive_token_refresh_failed",
                    level=logging.WARNING,
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(60)

    async def _load_catalog(
        self, client: CTraderProtocolClient, account: GatewayAccount
    ) -> SymbolCatalog:
        account_id = account.definition.ctid_trader_account_id
        listed = await client.request(
            ProtoOASymbolsListReq(ctidTraderAccountId=account_id, includeArchivedSymbols=False)
        )
        light_by_name = {str(item.symbolName): item for item in listed.symbol}
        missing = sorted(
            broker
            for broker in account.definition.instruments.values()
            if broker not in light_by_name
        )
        if missing:
            raise SymbolResolutionError(
                f"account {account.definition.alias} does not expose symbols {missing}"
            )
        selected = [light_by_name[name] for name in account.definition.instruments.values()]
        detailed_response = await client.request(
            ProtoOASymbolByIdReq(
                ctidTraderAccountId=account_id,
                symbolId=[int(item.symbolId) for item in selected],
            )
        )
        details = {int(item.symbolId): item for item in detailed_response.symbol}
        entries: list[SymbolInfo] = []
        for canonical, broker_name in account.definition.instruments.items():
            light = light_by_name[broker_name]
            detail = details[int(light.symbolId)]
            entries.append(
                SymbolInfo(
                    symbol=canonical,
                    symbol_id=int(light.symbolId),
                    digits=int(detail.digits),
                    enabled=bool(light.enabled),
                    description=str(light.description) or None,
                    lot_size=_field_int(detail, "lotSize"),
                    min_volume=_field_int(detail, "minVolume"),
                    max_volume=_field_int(detail, "maxVolume"),
                    step_volume=_field_int(detail, "stepVolume"),
                    sl_distance=_field_int(detail, "slDistance"),
                    trading_mode=_field_int(detail, "tradingMode"),
                    guaranteed_stop_loss=bool(detail.guaranteedStopLoss),
                )
            )
        return SymbolCatalog(entries)

    async def _reconcile_account(
        self, client: CTraderProtocolClient, account: GatewayAccount
    ) -> None:
        response = await client.request(
            ProtoOAReconcileReq(
                ctidTraderAccountId=account.definition.ctid_trader_account_id,
                returnProtectionOrders=False,
            )
        )
        account.positions = {int(item.positionId): item for item in response.position}
        account.orders = {int(item.orderId): item for item in response.order}
        account.reconciled = True
        if self.on_reconciled is not None:
            self.on_reconciled(account.definition.alias)

    def _on_event(self, environment: str, message: Message, client_msg_id: str | None) -> None:
        try:
            if isinstance(message, ProtoOASpotEvent):
                self._handle_spot(message)
            elif isinstance(message, ProtoOAExecutionEvent | ProtoOAOrderErrorEvent):
                self._handle_execution(message, client_msg_id)
            elif isinstance(message, ProtoOAAccountsTokenInvalidatedEvent):
                self._tokens.invalidate()
                client = self._clients.get(environment)
                if client is not None:
                    asyncio.create_task(client.close())
            elif isinstance(message, ProtoOAClientDisconnectEvent):
                client = self._clients.get(environment)
                if client is not None:
                    asyncio.create_task(client.close())
        except Exception as exc:
            log_event(
                "gateway_event_handling_failed",
                level=logging.ERROR,
                environment=environment,
                reason=type(exc).__name__,
            )

    def _handle_spot(self, event: ProtoOASpotEvent) -> None:
        account = self._by_id.get(int(event.ctidTraderAccountId))
        if account is None or account.catalog is None or not account.catalog.has_id(event.symbolId):
            return
        canonical = account.catalog.name_for_id(event.symbolId)
        tick = decode_spot(
            event,
            symbol=canonical,
            digits=account.catalog.digits_for_id(event.symbolId),
            previous=account.hub.last_tick(canonical),
        )
        if tick is not None:
            account.hub.publish_tick(tick)

    def _handle_execution(
        self,
        event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent,
        client_msg_id: str | None = None,
    ) -> None:
        account = self._by_id.get(int(event.ctidTraderAccountId))
        if account is None:
            return
        self._update_execution_cache(account, event)
        if self.on_execution_event is not None:
            self.on_execution_event(account.definition.alias, event, client_msg_id)

    @staticmethod
    def _update_execution_cache(
        account: GatewayAccount, event: ProtoOAExecutionEvent | ProtoOAOrderErrorEvent
    ) -> None:
        if isinstance(event, ProtoOAExecutionEvent):
            if event.HasField("position"):
                position_id = int(event.position.positionId)
                position_status = ProtoOAPositionStatus.Name(int(event.position.positionStatus))
                if position_status == "POSITION_STATUS_CLOSED":
                    account.positions.pop(position_id, None)
                else:
                    account.positions[position_id] = event.position
            if event.HasField("order"):
                order_id = int(event.order.orderId)
                execution_type = int(event.executionType)
                if execution_type in {
                    ProtoOAExecutionType.Value("ORDER_CANCELLED"),
                    ProtoOAExecutionType.Value("ORDER_EXPIRED"),
                }:
                    account.orders.pop(order_id, None)
                else:
                    account.orders[order_id] = event.order

    async def request(
        self, account_alias: str, message: Message, correlation_id: str | None = None
    ) -> Message:
        account = self.account(account_alias)
        environment = account.definition.environment
        if not self.account_ready(account_alias):
            raise CTraderError("NOT_CONNECTED", f"account {account_alias} is not ready")
        response = await self._enqueue_request(
            environment,
            message,
            priority=0,
            historical=False,
            correlation_id=correlation_id,
        )
        if isinstance(response, ProtoOAExecutionEvent | ProtoOAOrderErrorEvent):
            self._update_execution_cache(account, response)
        return response

    async def _enqueue_request(
        self,
        environment: str,
        message: Message,
        *,
        priority: int,
        historical: bool,
        correlation_id: str | None = None,
    ) -> Message:
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        await self._request_queues[environment].put(
            (
                priority,
                next(self._request_sequence),
                historical,
                message,
                correlation_id,
                future,
            )
        )
        return await future

    async def _request_worker(self, environment: str) -> None:
        queue = self._request_queues[environment]
        while True:
            _priority, _sequence, historical, message, correlation_id, future = await queue.get()
            if future.cancelled():
                queue.task_done()
                continue
            try:
                if historical:
                    await self._throttle_historical(environment)
                else:
                    await self._throttle(environment)
                client = self._clients.get(environment)
                if client is None or not self._environment_ready[environment].is_set():
                    raise CTraderError("NOT_CONNECTED", f"{environment} is not ready")
                response = await client.request(message, client_msg_id=correlation_id)
                if not future.done():
                    future.set_result(response)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                queue.task_done()

    async def _throttle(self, environment: str) -> None:
        interval = 1 / self.settings.non_historical_requests_per_second
        async with self._request_locks[environment]:
            loop = asyncio.get_running_loop()
            delay = self._last_request[environment] + interval - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request[environment] = loop.time()

    async def fetch_candles(
        self,
        *,
        account_alias: str,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> tuple[Candle, ...]:
        account = self.account(account_alias)
        if account.catalog is None:
            raise CTraderError("NOT_CONNECTED", f"account {account_alias} is not ready")
        info = account.catalog.info(symbol)
        environment = account.definition.environment
        end = to or datetime.now(UTC)
        collected: dict[datetime, Candle] = {}
        while len(collected) < count:
            response = await self._enqueue_request(
                environment,
                ProtoOAGetTrendbarsReq(
                    ctidTraderAccountId=account.definition.ctid_trader_account_id,
                    period=ProtoOATrendbarPeriod.Value(timeframe.value),
                    symbolId=info.symbol_id,
                    toTimestamp=int(end.timestamp() * 1000),
                    count=min(count - len(collected) + 1, 1000),
                ),
                priority=10,
                historical=True,
            )
            page = decode_trendbars(
                response.trendbar,
                symbol=symbol,
                period=timeframe.value,
                digits=info.digits,
            )
            if not page:
                break
            for candle in page:
                collected[candle.ts] = candle
            if not response.hasMore or len(collected) >= count:
                break
            next_end = min(collected) - period_duration_for(timeframe)
            if next_end >= end:
                break
            end = next_end
        ordered = sorted(collected.values(), key=lambda item: item.ts)
        return tuple(ordered[-count:])

    async def _throttle_historical(self, environment: str) -> None:
        interval = 1 / self.settings.historical_requests_per_second
        async with self._historical_locks[environment]:
            loop = asyncio.get_running_loop()
            delay = self._last_historical[environment] + interval - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_historical[environment] = loop.time()

    def list_orders(self, alias: str) -> list[BrokerOrder]:
        account = self.account(alias)
        return [self._normalize_order(account, order) for order in account.orders.values()]

    def list_positions(self, alias: str) -> list[BrokerPosition]:
        account = self.account(alias)
        return [
            self._normalize_position(account, position) for position in account.positions.values()
        ]

    def _normalize_order(self, account: GatewayAccount, order: Message) -> BrokerOrder:
        info = account.catalog.info(account.catalog.name_for_id(order.tradeData.symbolId))  # type: ignore[union-attr]
        return BrokerOrder(
            account=account.definition.alias,
            order_id=int(order.orderId),
            position_id=int(order.positionId) if order.HasField("positionId") else None,
            client_order_id=str(order.clientOrderId) if order.HasField("clientOrderId") else None,
            instrument=info.symbol,
            volume_lots=_protocol_to_lots(int(order.tradeData.volume), info.lot_size),
            state=ProtoOAOrderStatus.Name(int(order.orderStatus)),
        )

    def _normalize_position(self, account: GatewayAccount, position: Message) -> BrokerPosition:
        info = account.catalog.info(account.catalog.name_for_id(position.tradeData.symbolId))  # type: ignore[union-attr]
        return BrokerPosition(
            account=account.definition.alias,
            position_id=int(position.positionId),
            instrument=info.symbol,
            volume_lots=_protocol_to_lots(int(position.tradeData.volume), info.lot_size),
            direction=Direction.BUY if int(position.tradeData.tradeSide) == 1 else Direction.SELL,
            price=Decimal(str(position.price)) if position.HasField("price") else None,
            stop_loss=(Decimal(str(position.stopLoss)) if position.HasField("stopLoss") else None),
            take_profit=(
                Decimal(str(position.takeProfit)) if position.HasField("takeProfit") else None
            ),
        )

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        accounts = {
            alias: {
                "environment": account.definition.environment,
                "connected": self._environment_ready[account.definition.environment].is_set(),
                "catalog_loaded": account.catalog is not None,
                "reconciled": account.reconciled,
                "symbols": len(account.catalog) if account.catalog else 0,
                "reconnects": self._reconnects[account.definition.environment],
            }
            for alias, account in self._accounts.items()
        }
        return self.is_ready, {"accounts": accounts, "connected": self.is_ready}

    def _environment_accounts(self, environment: str) -> list[GatewayAccount]:
        return [
            account
            for account in self._accounts.values()
            if account.definition.environment == environment
        ]


def protobuf_dict(message: Message) -> dict[str, Any]:
    return MessageToDict(message, preserving_proto_field_name=True)


def _field_int(message: Message, field: str) -> int | None:
    return int(getattr(message, field)) if message.HasField(field) else None


def _protocol_to_lots(volume: int, lot_size: int | None) -> Decimal | None:
    if not lot_size:
        return None
    return Decimal(volume) / Decimal(lot_size)


def period_duration_for(timeframe: Timeframe) -> timedelta:
    return period_duration(timeframe.value)
