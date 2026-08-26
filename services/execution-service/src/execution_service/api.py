from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from ta_contracts import (
    AmendOrderRequest,
    BrokerOrder,
    BrokerPosition,
    CancelOrderRequest,
    CandlesResponse,
    ClosePositionRequest,
    OperationResponse,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
    SymbolsResponse,
    Tick,
    Timeframe,
)
from ta_core import COMMON_ERRORS, ErrorResponse, HealthResponse, create_base_app
from ta_store import ExecutionRepository

from . import compat
from .adapters.ctrader.gateway import CTraderGateway
from .adapters.ctrader.session import CTraderSession
from .adapters.mt5.mt5_adapter import MT5Adapter
from .config import Settings, load_settings
from .errors import ServiceError
from .hub import MarketDataHub
from .logging_config import configure_file_logs, configure_logging, log_event
from .market_data_service import GatewayMarketDataService, MarketDataService, parse_to_timestamp
from .service import ExecutionService
from .stream import SSE_HEADERS, tick_stream


class AccountStatus(BaseModel):
    alias: str
    ctid_trader_account_id: int
    environment: str
    is_live: bool
    connected: bool
    reconciled: bool
    broker_access_rights: str | None
    available_for_trading: bool
    order_entry_enabled: bool
    position_close_enabled: bool


class AccountsResponse(BaseModel):
    profile: str | None
    accounts: list[AccountStatus]
    unconfigured_authorized_accounts: int
    unavailable_authorized_accounts: int


def create_app(
    settings: Settings | None = None,
    session: CTraderSession | None = None,
    gateway: CTraderGateway | None = None,
    repository: ExecutionRepository | None = None,
    mt5_adapter: MT5Adapter | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)

    # One process, one or more brokers. Each adapter is constructed only when
    # ADAPTERS names it, which is what lets the same codebase run on macOS
    # against cTrader and on Windows against MetaTrader 5.
    mt5_stack = compat.build_stack(settings, mt5_adapter) if "mt5" in settings.adapters else None

    ctrader_enabled = "ctrader" in settings.adapters
    execution_service: ExecutionService | None = None
    market_data: GatewayMarketDataService | MarketDataService | None = None
    hub: MarketDataHub | None = None
    if ctrader_enabled:
        if settings.gateway_enabled:
            gateway = gateway or CTraderGateway(settings)
            repository = repository or ExecutionRepository(settings.execution_database_path)
            repository.initialize()
            market_data = GatewayMarketDataService(settings, gateway)
            execution_service = ExecutionService(settings, gateway, repository)
            hub = gateway.account(gateway.default_account_alias).hub
        else:
            # An injected session already owns a hub; reusing it is what keeps the
            # API reading the same quotes the session publishes.
            if session is None:
                hub = MarketDataHub(queue_size=settings.subscriber_queue_size)
                session = CTraderSession(settings, hub)
            else:
                hub = session.hub
            market_data = MarketDataService(settings, session, hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not ctrader_enabled:
            pass
        elif gateway is not None:
            log_event(
                "gateway_starting",
                profile=settings.profile,
                accounts=list(gateway.aliases()),
            )
            await gateway.start()
            ready = await gateway.wait_ready(timeout_seconds=settings.startup_ready_timeout_seconds)
        else:
            assert session is not None
            log_event(
                "service_starting",
                profile=settings.profile,
                environment=settings.environment,
                host=settings.resolved_host,
                port=settings.ctrader_port,
                account_id=settings.account_id,
                symbols=sorted(settings.symbols),
            )
            await session.start()
            ready = await session.wait_ready(timeout_seconds=settings.startup_ready_timeout_seconds)
        if ctrader_enabled:
            log_event(
                "startup_handshake_completed" if ready else "startup_handshake_pending",
                level=logging.INFO if ready else logging.WARNING,
                ready=ready,
            )
        if mt5_stack is not None:
            await compat.startup(mt5_stack)
        try:
            yield
        finally:
            if mt5_stack is not None:
                await compat.shutdown(mt5_stack)
            log_event("service_stopping", profile=settings.profile)
            if gateway is not None:
                await gateway.close()
            elif session is not None:
                await session.close()

    def _readiness() -> tuple[bool, dict[str, Any]]:
        """Readiness of whichever adapter this process actually runs."""
        if market_data is not None:
            return market_data.readiness()
        if mt5_stack is not None:
            # The adapter's own probe, not a proxy for it: it also gates on
            # trading_enabled and on the terminal's reported trade permission.
            return mt5_stack.service.readiness()
        return False, {"reason": "no adapter is configured"}

    async def _on_error(event: str, request: Request, status_code: int, error: Any) -> None:
        """mt5-trader notified an operator on a rejected request; keep that.

        401 is excluded because an unauthenticated probe is noise, and a failed
        POST /v1/signals is skipped because the signal path already notifies its
        own outcome — without this the operator gets the same rejection twice.
        """
        if mt5_stack is None:
            return
        signal_outcome_notified = (
            request.method == "POST" and request.url.path.rstrip("/") == "/v1/signals"
        )
        if status_code == 401 or (event == "service_error_response" and signal_outcome_notified):
            return
        await mt5_stack.notifications.notify_request_failure(
            event=event,
            path=request.url.path,
            status_code=status_code,
            client=request.client.host if request.client else None,
            error=error,
        )

    app, authenticate = create_base_app(
        settings,
        title="Execution Service",
        version="0.2.0",
        lifespan=lifespan,
        readiness=_readiness,
        # mt5-trader printed handler events and notified on them; ctrader-markets
        # did neither. Follow whichever adapter this process is running.
        error_console=mt5_stack is not None,
        on_error=_on_error if mt5_stack is not None else None,
    )
    app.description = (
        "Account-qualified market data plus durable, idempotent multi-account trade "
        "execution. Run with exactly one worker: the process centrally owns the OAuth "
        "token and at most one connection per demo/live environment."
    )
    app.state.settings = settings
    app.state.session = session
    app.state.gateway = gateway
    app.state.repository = repository
    app.state.execution_service = execution_service
    app.state.hub = hub
    app.state.market_data = market_data
    app.state.mt5 = mt5_stack

    if mt5_stack is not None:
        compat.register_routes(app, mt5_stack, authenticate)

    common_errors = COMMON_ERRORS

    if ctrader_enabled:

        @app.get(
            "/v1/market-data/tick",
            response_model=Tick,
            responses=common_errors,
            dependencies=[Depends(authenticate)],
        )
        async def get_tick(
            symbol: str = Query(..., min_length=1, max_length=64),
            account: str | None = Query(default=None, min_length=1, max_length=63),
        ) -> Tick:
            if isinstance(market_data, GatewayMarketDataService):
                return market_data.get_tick(symbol, account)
            return market_data.get_tick(symbol)

        @app.get(
            "/v1/market-data/candles",
            response_model=CandlesResponse,
            responses=common_errors,
            dependencies=[Depends(authenticate)],
        )
        async def get_candles(
            symbol: str = Query(..., min_length=1, max_length=64),
            timeframe: Timeframe = Query(default=Timeframe.H1),  # noqa: B008
            count: int = Query(default=500, gt=0),
            to: str | None = Query(
                default=None,
                description="ISO-8601 upper bound. Defaults to now. Only closed bars are returned.",
            ),
            account: str | None = Query(default=None, min_length=1, max_length=63),
        ) -> CandlesResponse:
            if isinstance(market_data, GatewayMarketDataService):
                return await market_data.get_candles(
                    symbol, timeframe, count, parse_to_timestamp(to), account
                )
            return await market_data.get_candles(symbol, timeframe, count, parse_to_timestamp(to))

        @app.get(
            "/v1/symbols",
            response_model=SymbolsResponse,
            responses=common_errors,
            dependencies=[Depends(authenticate)],
        )
        async def list_symbols(
            account: str | None = Query(default=None, min_length=1, max_length=63),
        ) -> SymbolsResponse:
            if isinstance(market_data, GatewayMarketDataService):
                return market_data.list_symbols(account)
            return market_data.list_symbols()

        @app.get("/v1/stream/ticks", responses=common_errors, dependencies=[Depends(authenticate)])
        async def stream_ticks(
            symbols: str | None = Query(
                default=None,
                description="Comma-separated subset. Omit for every configured symbol.",
            ),
            account: str | None = Query(default=None, min_length=1, max_length=63),
        ) -> EventSourceResponse:
            stream_hub = hub
            if isinstance(market_data, GatewayMarketDataService):
                stream_hub, requested = market_data.resolve_stream(symbols, account)
            else:
                requested = market_data.resolve_stream_symbols(symbols)
            log_event(
                "stream_subscriber_opened",
                console=False,
                symbols=sorted(requested) if requested else None,
            )
            return EventSourceResponse(
                tick_stream(stream_hub, requested),
                ping=int(settings.sse_keepalive_seconds),
                headers=SSE_HEADERS,
            )

        @app.get(
            "/health/trading-ready",
            response_model=HealthResponse,
            responses={503: {"model": HealthResponse}},
        )
        async def trading_readiness() -> HealthResponse | JSONResponse:
            if gateway is None or repository is None:
                details = {"reason": "multi-account execution gateway is not configured"}
                body = HealthResponse(status="not_ready", details=details)
                return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
            ready, details = gateway.readiness()
            details["database_healthy"] = repository.is_healthy()
            details["trading_enabled"] = settings.trading_enabled
            details["live_trading_enabled"] = settings.live_trading_enabled
            ready = ready and repository.is_healthy() and settings.trading_enabled
            has_live_accounts = any(
                gateway.account(alias).definition.environment == "live"
                for alias in gateway.aliases()
            )
            if has_live_accounts and not settings.live_trading_enabled:
                ready = False
                details["reason"] = "LIVE_TRADING_ENABLED is false with enabled live accounts"
            body = HealthResponse(status="ready" if ready else "not_ready", details=details)
            if ready:
                return body
            return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    if execution_service is not None and gateway is not None:

        def operation_response(response: OperationResponse) -> JSONResponse:
            pending = response.state in {OperationState.PENDING, OperationState.UNKNOWN}
            status_code = 202 if pending else 201
            headers = {"Location": f"/v1/operations/{response.operation_id}"} if pending else None
            return JSONResponse(
                status_code=status_code,
                content=response.model_dump(mode="json"),
                headers=headers,
            )

        @app.post(
            "/v1/orders",
            response_model=OperationResponse,
            status_code=201,
            responses={**common_errors, 202: {"model": OperationResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def place_order(request: OrderRequest) -> JSONResponse:
            return operation_response(await execution_service.place_order(request))

        @app.post(
            "/v1/orders/amend",
            response_model=OperationResponse,
            status_code=201,
            responses={**common_errors, 202: {"model": OperationResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def amend_order(request: AmendOrderRequest) -> JSONResponse:
            return operation_response(await execution_service.amend_order(request))

        @app.post(
            "/v1/orders/cancel",
            response_model=OperationResponse,
            status_code=201,
            responses={**common_errors, 202: {"model": OperationResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def cancel_order(request: CancelOrderRequest) -> JSONResponse:
            return operation_response(await execution_service.cancel_order(request))

        @app.post(
            "/v1/positions/protection",
            response_model=OperationResponse,
            status_code=201,
            responses={**common_errors, 202: {"model": OperationResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def amend_position(request: PositionProtectionRequest) -> JSONResponse:
            return operation_response(await execution_service.amend_position(request))

        @app.post(
            "/v1/positions/close",
            response_model=OperationResponse,
            status_code=201,
            responses={**common_errors, 202: {"model": OperationResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def close_position(request: ClosePositionRequest) -> JSONResponse:
            return operation_response(await execution_service.close_position(request))

        @app.get(
            "/v1/operations/{operation_id}",
            response_model=OperationResponse,
            responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
            dependencies=[Depends(authenticate)],
        )
        async def operation_status(operation_id: UUID) -> OperationResponse:
            return execution_service.status(operation_id)

        @app.get(
            "/v1/accounts",
            response_model=AccountsResponse,
            dependencies=[Depends(authenticate)],
        )
        async def accounts() -> AccountsResponse:
            return AccountsResponse(
                profile=settings.profile,
                accounts=[
                    AccountStatus.model_validate(item) for item in gateway.account_statuses()
                ],
                unconfigured_authorized_accounts=gateway.unconfigured_authorized_account_count,
                unavailable_authorized_accounts=gateway.unavailable_authorized_account_count,
            )

        @app.get(
            "/v1/accounts/{alias}/orders",
            response_model=list[BrokerOrder],
            dependencies=[Depends(authenticate)],
        )
        async def account_orders(alias: str) -> list[BrokerOrder]:
            try:
                return gateway.list_orders(alias)
            except KeyError as exc:
                raise ServiceError(404, "account_not_found", str(exc)) from exc

        @app.get(
            "/v1/accounts/{alias}/positions",
            response_model=list[BrokerPosition],
            dependencies=[Depends(authenticate)],
        )
        async def account_positions(alias: str) -> list[BrokerPosition]:
            try:
                return gateway.list_positions(alias)
            except KeyError as exc:
                raise ServiceError(404, "account_not_found", str(exc)) from exc

    return app
