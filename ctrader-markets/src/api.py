from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from config import Settings, load_settings
from ctrader.gateway import CTraderGateway
from ctrader.session import CTraderSession
from errors import ServiceError
from execution_repository import ExecutionRepository
from execution_service import ExecutionService
from hub import MarketDataHub
from logging_config import configure_file_logs, configure_logging, log_event
from market_data_service import GatewayMarketDataService, MarketDataService, parse_to_timestamp
from models import (
    AmendOrderRequest,
    BrokerOrder,
    BrokerPosition,
    CancelOrderRequest,
    CandlesResponse,
    ClosePositionRequest,
    ErrorResponse,
    HealthResponse,
    OperationResponse,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
    SymbolsResponse,
    Tick,
    Timeframe,
)
from stream import SSE_HEADERS, tick_stream


def create_app(
    settings: Settings | None = None,
    session: CTraderSession | None = None,
    gateway: CTraderGateway | None = None,
    repository: ExecutionRepository | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)

    execution_service: ExecutionService | None = None
    if settings.gateway_enabled:
        gateway = gateway or CTraderGateway(settings)
        repository = repository or ExecutionRepository(settings.execution_database_path)
        repository.initialize()
        market_data: GatewayMarketDataService | MarketDataService = GatewayMarketDataService(
            settings, gateway
        )
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
        if gateway is not None:
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
        log_event(
            "startup_handshake_completed" if ready else "startup_handshake_pending",
            level=logging.INFO if ready else logging.WARNING,
            ready=ready,
        )
        try:
            yield
        finally:
            log_event("service_stopping", profile=settings.profile)
            if gateway is not None:
                await gateway.close()
            else:
                assert session is not None
                await session.close()

    app = FastAPI(
        title="cTrader Markets and Execution Gateway",
        version="0.2.0",
        description=(
            "Account-qualified cTrader market data plus durable, idempotent multi-account "
            "trade execution. Run with exactly one worker: the process centrally owns the "
            "OAuth token and at most one connection per demo/live environment."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session = session
    app.state.gateway = gateway
    app.state.repository = repository
    app.state.execution_service = execution_service
    app.state.hub = hub
    app.state.market_data = market_data

    async def authenticate(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = settings.api_key.get_secret_value()
        if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
            log_event(
                "authentication_failed",
                level=logging.WARNING,
                console=False,
                path=request.url.path,
                client=request.client.host if request.client else None,
                api_key_present=x_api_key is not None,
            )
            raise ServiceError(401, "unauthorized", "A valid X-API-Key header is required")

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        log_event(
            "service_error_response",
            level=logging.WARNING if exc.status_code < 500 else logging.ERROR,
            console=False,
            path=request.url.path,
            method=request.method,
            client=request.client.host if request.client else None,
            status_code=exc.status_code,
            error=exc.as_dict(),
        )
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": [str(part) for part in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        log_event(
            "request_validation_failed",
            level=logging.WARNING,
            console=False,
            path=request.url.path,
            client=request.client.host if request.client else None,
            errors=details,
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request payload is invalid",
                    "details": details,
                }
            },
        )

    common_errors: dict[int | str, dict[str, Any]] = {
        401: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    }

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
            default=None, description="Comma-separated subset. Omit for every configured symbol."
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

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def readiness() -> HealthResponse | JSONResponse:
        ready, details = market_data.readiness()
        body = HealthResponse(status="ready" if ready else "not_ready", details=details)
        if ready:
            return body
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    @app.get(
        "/health/trading-ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def trading_readiness() -> HealthResponse | JSONResponse:
        if gateway is None or repository is None:
            details = {"reason": "multi-account execution gateway is not configured"}
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="not_ready", details=details).model_dump(mode="json"),
            )
        ready, details = gateway.readiness()
        details["database_healthy"] = repository.is_healthy()
        details["trading_enabled"] = settings.trading_enabled
        details["live_trading_enabled"] = settings.live_trading_enabled
        ready = ready and repository.is_healthy() and settings.trading_enabled
        has_live_accounts = any(
            account.environment == "live" for account in settings.enabled_accounts
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
