from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import ServiceError
from .models import (
    ErrorResponse,
    HealthResponse,
    SignalRequest,
    SignalResponse,
    SignalStatus,
)
from .mt5_adapter import MT5Adapter, RealMT5Adapter
from .repository import SignalRepository
from .service import SignalExecutionService


def create_app(
    settings: Settings | None = None,
    adapter: MT5Adapter | None = None,
) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    adapter = adapter or RealMT5Adapter()
    repository = SignalRepository(settings.database_path)
    service = SignalExecutionService(settings, adapter, repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(repository.initialize)
        try:
            initialized = await asyncio.to_thread(adapter.initialize, settings)
            app.state.mt5_initialized = initialized
            if initialized:
                await asyncio.to_thread(service.reconcile_startup)
        except Exception:
            app.state.mt5_initialized = False
        yield
        if app.state.mt5_initialized:
            await asyncio.to_thread(adapter.shutdown)

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

    async def authenticate(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        expected = settings.api_key.get_secret_value()
        if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
            raise ServiceError(401, "unauthorized", "A valid X-API-Key header is required")

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
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
        return await service.status(signal_id)

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
