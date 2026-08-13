from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .errors import ServiceError
from .logging_config import configure_file_logs, configure_logging, log_event
from .market_data_service import MarketDataService
from .models import (
    CandlesResponse,
    ErrorResponse,
    HealthResponse,
    SignalRequest,
    SignalResponse,
    SignalStatus,
    TickResponse,
    Timeframe,
)
from .mt5_adapter import MT5Adapter, RealMT5Adapter
from .notification_client import NotificationClient
from .repository import SignalRepository
from .service import SignalExecutionService


def create_app(
    settings: Settings | None = None,
    adapter: MT5Adapter | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    signal_file_log = configure_file_logs(settings.signals_log_path)
    adapter = adapter or RealMT5Adapter()
    repository = SignalRepository(settings.database_path)
    notification_client = NotificationClient(settings)
    service = SignalExecutionService(
        settings,
        adapter,
        repository,
        signal_file_log=signal_file_log,
        notification_client=notification_client,
    )
    market_data_service = MarketDataService(settings, adapter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_event(
            "service_starting",
            profile=settings.profile,
            terminal_path=str(settings.terminal_path),
            expected_login=settings.login,
            server=settings.server,
            database_path=str(settings.database_path),
            allowed_symbols=sorted(settings.allowed_symbols),
            allowed_signal_sources=sorted(settings.allowed_signal_sources),
            maximum_volume=str(settings.maximum_volume),
            magic_number=settings.magic_number,
            trading_enabled=settings.trading_enabled,
        )
        await asyncio.to_thread(repository.initialize)
        log_event(
            "audit_database_initialized",
            console=False,
            database_path=str(settings.database_path),
        )
        try:
            log_event("mt5_initialize_started", console=False)
            initialized = await asyncio.to_thread(adapter.initialize, settings)
            app.state.mt5_initialized = initialized
            log_event("mt5_initialize_completed", console=False, initialized=initialized)
            if initialized:
                await asyncio.to_thread(service.reconcile_startup)
                probe_results = await market_data_service.probe_symbols()
                symbols_ok = sum(1 for result in probe_results if result.get("ok"))
                log_event(
                    "market_data_probe_completed",
                    profile=settings.profile,
                    symbols_total=len(probe_results),
                    symbols_ok=symbols_ok,
                    symbols_failed=len(probe_results) - symbols_ok,
                    results=probe_results,
                )
        except Exception as exc:
            app.state.mt5_initialized = False
            log_event(
                "mt5_initialize_failed",
                level=40,
                console=False,
                exc_info=True,
                reason=type(exc).__name__,
            )
        yield
        log_event("service_stopping", mt5_initialized=app.state.mt5_initialized)
        if app.state.mt5_initialized:
            await asyncio.to_thread(adapter.shutdown)
            log_event("mt5_shutdown_completed", console=False)

    app = FastAPI(
        title="MT5 Signal Execution Service",
        version="0.1.0",
        description=(
            "Single-account, idempotent execution of authenticated market, limit, and stop "
            "signals through a local MetaTrader 5 terminal. Run with exactly one worker."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.adapter = adapter
    app.state.repository = repository
    app.state.service = service
    app.state.market_data_service = market_data_service
    app.state.notification_client = notification_client

    async def authenticate(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = settings.api_key.get_secret_value()
        if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
            log_event(
                "authentication_failed",
                level=30,
                console=False,
                path=request.url.path,
                client=request.client.host if request.client else None,
                api_key_present=x_api_key is not None,
            )
            raise ServiceError(401, "unauthorized", "A valid X-API-Key header is required")

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        client_host = request.client.host if request.client else None
        log_event(
            "service_error_response",
            level=30 if exc.status_code < 500 else 40,
            console=True,
            file=True,
            path=request.url.path,
            method=request.method,
            client=client_host,
            status_code=exc.status_code,
            error=exc.as_dict(),
        )
        signal_outcome_notified = (
            request.method == "POST" and request.url.path.rstrip("/") == "/v1/signals"
        )
        if exc.status_code != 401 and not signal_outcome_notified:
            await notification_client.notify_request_failure(
                event="service_error_response",
                path=request.url.path,
                status_code=exc.status_code,
                client=client_host,
                error=exc.as_dict(),
            )
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for error in exc.errors():
            details.append(
                {
                    "location": [str(part) for part in error["loc"]],
                    "message": error["msg"],
                    "type": error["type"],
                }
            )
        client_host = request.client.host if request.client else None
        log_event(
            "request_validation_failed",
            level=30,
            console=True,
            file=True,
            path=request.url.path,
            client=client_host,
            errors=details,
        )
        await notification_client.notify_request_failure(
            event="request_validation_failed",
            path=request.url.path,
            status_code=422,
            client=client_host,
            error=details,
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

    @app.post(
        "/v1/signals",
        response_model=SignalResponse,
        responses=common_errors,
        dependencies=[Depends(authenticate)],
    )
    async def submit_signal(signal: SignalRequest) -> SignalResponse:
        return await service.execute(signal)

    @app.get(
        "/v1/signals/{signal_id}",
        response_model=SignalStatus,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
        dependencies=[Depends(authenticate)],
    )
    async def get_signal(signal_id: UUID) -> SignalStatus:
        status = await service.status(signal_id)
        log_event(
            "signal_status_retrieved",
            console=False,
            signal_id=str(signal_id),
            state=status.state.value,
        )
        return status

    @app.get(
        "/v1/market-data/candles",
        response_model=CandlesResponse,
        responses={
            401: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        dependencies=[Depends(authenticate)],
    )
    async def get_candles(
        quote: str = Query(..., min_length=1, max_length=64),
        timeframe: Timeframe = Query(default=Timeframe.M1),  # noqa: B008
        count: int = Query(default=500, gt=0),
    ) -> CandlesResponse:
        return await market_data_service.get_candles(quote, timeframe, count)

    @app.get(
        "/v1/market-data/tick",
        response_model=TickResponse,
        responses={
            401: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        dependencies=[Depends(authenticate)],
    )
    async def get_tick(
        quote: str = Query(..., min_length=1, max_length=64),
    ) -> TickResponse:
        return await market_data_service.get_tick(quote)

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def readiness() -> HealthResponse | JSONResponse:
        ready, details = await service.readiness()
        body = HealthResponse(status="ready" if ready else "not_ready", details=details)
        if ready:
            return body
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    return app
