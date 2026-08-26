"""The FastAPI scaffolding both services had written out by hand.

`create_base_app` returns an app with the API-key dependency, both exception
handlers and the two health routes already wired. A service adds its own routes
and nothing else; every `create_app` in the repository reduces to this plus
routes.
"""

from __future__ import annotations

import hmac
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

ReadinessProbe = Callable[[], tuple[bool, dict[str, Any]]]


def create_base_app(
    settings: BaseServiceSettings,
    *,
    title: str,
    version: str = "0.1.0",
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    readiness: ReadinessProbe | None = None,
) -> tuple[FastAPI, Callable[..., Any]]:
    """Build the app and return it with the authentication dependency.

    The dependency is returned rather than applied globally because the health
    routes must stay reachable by an unauthenticated supervisor, and because
    some services expose a second, stricter dependency for write routes.
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

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="alive")

    if readiness is not None:

        @app.get(
            "/health/ready",
            response_model=HealthResponse,
            responses={503: {"model": HealthResponse}},
        )
        async def readiness_route() -> HealthResponse | JSONResponse:
            ready, details = readiness()
            body = HealthResponse(status="ready" if ready else "not_ready", details=details)
            if ready:
                return body
            return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    return app, authenticate


def authenticated(dependency: Callable[..., Any]) -> list[Any]:
    """Sugar for `dependencies=authenticated(auth)` on a route."""
    return [Depends(dependency)]
