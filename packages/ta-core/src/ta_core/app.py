"""The FastAPI scaffolding both services had written out by hand.

`create_base_app` returns an app with the API-key dependency, both exception
handlers and the two health routes already wired. A service adds its own routes
and nothing else; every `create_app` in the repository reduces to this plus
routes.
"""

from __future__ import annotations

import hmac
import inspect
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import ServiceError
from .logging_config import log_event
from .settings import BaseServiceSettings


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    details: dict[str, Any] | None = None


# Attached to every authenticated route so the generated OpenAPI documents the
# failures a client actually has to handle.
COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

# Either sync or async: cTrader's readiness is a plain call, MetaTrader 5's has
# to reach the terminal and is a coroutine. Supporting both here keeps that
# difference out of every service.
ReadinessProbe = Callable[[], Any]

# Called after an error has been logged, with (event, request, status_code,
# error_payload). mt5-trader used this point to notify on rejected signals; that
# is service policy, so it is a hook rather than something baked in here.
ErrorHook = Callable[[str, Request, int, Any], Any]


def create_base_app(
    settings: BaseServiceSettings,
    *,
    title: str,
    version: str = "0.1.0",
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    readiness: ReadinessProbe | None = None,
    error_console: bool = False,
    on_error: ErrorHook | None = None,
) -> tuple[FastAPI, Callable[..., Any]]:
    """Build the app and return it with the authentication dependency.

    The dependency is returned rather than applied globally because the health
    routes must stay reachable by an unauthenticated supervisor, and because
    some services expose a second, stricter dependency for write routes.

    `error_console` and `on_error` exist because the two services this replaces
    genuinely disagreed: ctrader-markets logged handler events with
    console=False, while mt5-trader printed them and also notified an operator.
    Both are defensible, so neither is hardcoded.
    """
    app = FastAPI(title=title, version=version, lifespan=lifespan)
    app.state.settings = settings

    async def authenticate(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = settings.api_key.get_secret_value()
        # compare_digest, not ==: an early-exit comparison leaks the key one
        # byte at a time to anyone who can time the response.
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
            console=error_console,
            path=request.url.path,
            method=request.method,
            client=request.client.host if request.client else None,
            status_code=exc.status_code,
            error=exc.as_dict(),
        )
        if on_error is not None:
            result = on_error("service_error_response", request, exc.status_code, exc.as_dict())
            if inspect.isawaitable(result):
                await result
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
            console=error_console,
            path=request.url.path,
            client=request.client.host if request.client else None,
            errors=details,
        )
        if on_error is not None:
            result = on_error("request_validation_failed", request, 422, details)
            if inspect.isawaitable(result):
                await result
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

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    if readiness is not None:

        @app.get(
            "/health/ready",
            response_model=HealthResponse,
            responses={503: {"model": HealthResponse}},
        )
        async def readiness_route() -> HealthResponse | JSONResponse:
            result = readiness()
            if inspect.isawaitable(result):
                result = await result
            ready, details = result
            body = HealthResponse(status="ready" if ready else "not_ready", details=details)
            if ready:
                return body
            return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    return app, authenticate


def authenticated(dependency: Callable[..., Any]) -> list[Any]:
    """Sugar for `dependencies=authenticated(auth)` on a route."""
    return [Depends(dependency)]
