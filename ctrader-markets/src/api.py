from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from config import Settings, load_settings
from ctrader.session import CTraderSession
from errors import ServiceError
from hub import MarketDataHub
from logging_config import configure_file_logs, configure_logging, log_event
from market_data_service import MarketDataService, parse_to_timestamp
from models import (
    CandlesResponse,
    ErrorResponse,
    HealthResponse,
    SymbolsResponse,
    Tick,
    Timeframe,
)
from stream import SSE_HEADERS, tick_stream


def create_app(
    settings: Settings | None = None,
    session: CTraderSession | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)

    # An injected session already owns a hub; reusing it is what keeps the API
    # reading the same quotes the session publishes.
    if session is None:
        hub = MarketDataHub(queue_size=settings.subscriber_queue_size)
        session = CTraderSession(settings, hub)
    else:
        hub = session.hub
    market_data = MarketDataService(settings, session, hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_event(
            "service_starting",
            profile=settings.profile,
            environment=settings.environment,
            host=settings.resolved_host,
            port=settings.ctrader_port,
            account_id=settings.account_id,
            symbols=sorted(settings.symbols),
            live_trendbar_periods=list(settings.live_trendbar_periods),
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
            await session.close()

    app = FastAPI(
        title="cTrader Markets",
        version="0.1.0",
        description=(
            "Profile-scoped market data from the cTrader Open API: live spot ticks over SSE, "
            "plus REST access to the latest tick, closed trendbars, and symbol metadata. "
            "Run with exactly one worker — the process owns a single broker connection."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session = session
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
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    }

    @app.get(
        "/v1/market-data/tick",
        response_model=Tick,
        responses=common_errors,
        dependencies=[Depends(authenticate)],
    )
    async def get_tick(symbol: str = Query(..., min_length=1, max_length=64)) -> Tick:
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
    ) -> CandlesResponse:
        return await market_data.get_candles(symbol, timeframe, count, parse_to_timestamp(to))

    @app.get(
        "/v1/symbols",
        response_model=SymbolsResponse,
        responses=common_errors,
        dependencies=[Depends(authenticate)],
    )
    async def list_symbols() -> SymbolsResponse:
        return market_data.list_symbols()

    @app.get("/v1/stream/ticks", responses=common_errors, dependencies=[Depends(authenticate)])
    async def stream_ticks(
        symbols: str | None = Query(
            default=None, description="Comma-separated subset. Omit for every configured symbol."
        ),
    ) -> EventSourceResponse:
        requested = market_data.resolve_stream_symbols(symbols)
        log_event(
            "stream_subscriber_opened",
            console=False,
            symbols=sorted(requested) if requested else None,
        )
        return EventSourceResponse(
            tick_stream(hub, requested),
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

    return app
