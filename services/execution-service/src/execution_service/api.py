from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ta_contracts import (
    AmendOrderRequest,
    BrokerOrder,
    BrokerPosition,
    CancelOrderRequest,
    ClosePositionRequest,
    OperationResponse,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
)
from ta_core import COMMON_ERRORS, ErrorResponse, HealthResponse, create_base_app
from ta_plugin_api import EXECUTION_GROUP, ExecutionProvider, load_providers
from ta_plugin_ctrader.gateway import CTraderGateway
from ta_plugin_mt5.terminal import MT5Adapter
from ta_store import ExecutionRepository, OcoGroupStore

from . import compat
from .config import Settings, load_settings
from .logging_config import configure_file_logs, configure_logging, log_event
from .oco import OcoCoordinator, OcoRouter
from .oco_routes import register_oco_routes
from .service import ExecutionService

if TYPE_CHECKING:
    from ta_plugin_ctrader.execution import CTraderExecution


class AccountStatus(BaseModel):
    alias: str
    provider: str
    # cTrader only; an MT5 account is the host's terminal and has no such ID.
    ctid_trader_account_id: int | None
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
    gateway: CTraderGateway | None = None,
    repository: ExecutionRepository | None = None,
    mt5_adapter: MT5Adapter | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)

    # One process, one or more brokers, one ledger. Each broker is discovered
    # through the ta.execution entry points and constructed only when ADAPTERS
    # names it, which is what lets the same codebase run on macOS against
    # cTrader and on Windows against MetaTrader 5. Market data is not served
    # here: that is market-data-service, with its own process and OAuth grant.
    repository = repository or ExecutionRepository(settings.execution_database_path)
    repository.initialize()
    oco_store = OcoGroupStore(repository.path)
    oco_store.initialize()
    discovered = load_providers(EXECUTION_GROUP, list(settings.adapters))

    providers: list[ExecutionProvider] = []
    ctrader: CTraderExecution | None = None
    if "ctrader" in settings.adapters:
        ctrader = discovered["ctrader"].execution(settings, gateway=gateway)
        gateway = ctrader.gateway
        providers.append(ctrader)
    mt5_stack = (
        compat.build_stack(settings, repository, oco_store, mt5_adapter)
        if "mt5" in settings.adapters
        else None
    )
    if mt5_stack is not None:
        providers.append(mt5_stack.provider)
    execution_service = ExecutionService(settings, providers, repository)

    # OCO runs only where a broker publishes a venue for it (MT5). Accounts on
    # other providers answer 501 rather than "unknown account".
    coordinators: dict[str, OcoCoordinator] = {}
    if mt5_stack is not None:
        venue = discovered["mt5"].oco(settings, mt5_stack.provider)
        coordinators[venue.account] = OcoCoordinator(settings, venue, oco_store)
    oco = OcoRouter(
        coordinators,
        {account for account in execution_service.accounts() if account not in coordinators},
        oco_store,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if ctrader is not None:
            log_event(
                "gateway_starting",
                profile=settings.profile,
                accounts=list(ctrader.accounts()),
            )
            await ctrader.start()
            ready = await ctrader.wait_ready(settings.startup_ready_timeout_seconds)
            log_event(
                "startup_handshake_completed" if ready else "startup_handshake_pending",
                level=logging.INFO if ready else logging.WARNING,
                ready=ready,
            )
        if mt5_stack is not None:
            await compat.startup(mt5_stack)
            if mt5_stack.initialized:
                await oco.start([mt5_stack.provider.account])
        reconciler = (
            asyncio.create_task(
                execution_service.run_reconciler(settings.reconcile_interval_seconds)
            )
            if settings.reconcile_interval_seconds > 0
            else None
        )
        try:
            yield
        finally:
            if reconciler is not None:
                reconciler.cancel()
                with suppress(asyncio.CancelledError):
                    await reconciler
            await oco.stop()
            if mt5_stack is not None:
                await compat.shutdown(mt5_stack)
            log_event("service_stopping", profile=settings.profile)
            if ctrader is not None:
                await ctrader.close()

    def _readiness() -> Any:
        """Readiness of whichever broker this process actually runs."""
        if ctrader is not None:
            return ctrader.readiness()
        if mt5_stack is not None:
            # The signal path's own probe: it also gates on trading_enabled,
            # the ledger, and the terminal's reported trade permission.
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
        version="0.3.0",
        lifespan=lifespan,
        readiness=_readiness,
        # mt5-trader printed handler events and notified on them; ctrader-markets
        # did neither. Follow whichever adapter this process is running.
        error_console=mt5_stack is not None,
        on_error=_on_error if mt5_stack is not None else None,
    )
    app.description = (
        "Durable, idempotent multi-account trade execution. Run with exactly one "
        "worker: the process centrally owns the OAuth token and at most one "
        "connection per demo/live environment. Market data is market-data-service."
    )
    app.state.settings = settings
    app.state.gateway = gateway
    app.state.repository = repository
    app.state.execution_service = execution_service
    app.state.mt5 = mt5_stack
    app.state.oco = oco

    if mt5_stack is not None:
        compat.register_routes(app, mt5_stack, authenticate)
    register_oco_routes(app, oco, authenticate, mt5_aliases=mt5_stack is not None)

    common_errors = COMMON_ERRORS

    if ctrader is not None:

        @app.get(
            "/health/trading-ready",
            response_model=HealthResponse,
            responses={503: {"model": HealthResponse}},
        )
        async def trading_readiness() -> HealthResponse | JSONResponse:
            ready, details = ctrader.readiness()
            database_healthy = repository.is_healthy()
            details["database_healthy"] = database_healthy
            details["trading_enabled"] = settings.trading_enabled
            details["live_trading_enabled"] = settings.live_trading_enabled
            ready = ready and database_healthy and settings.trading_enabled
            if ctrader.has_live_accounts() and not settings.live_trading_enabled:
                ready = False
                details["reason"] = "LIVE_TRADING_ENABLED is false with enabled live accounts"
            body = HealthResponse(status="ready" if ready else "not_ready", details=details)
            if ready:
                return body
            return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    if providers:

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
                    AccountStatus.model_validate(item)
                    for item in execution_service.account_statuses()
                ],
                unconfigured_authorized_accounts=(
                    gateway.unconfigured_authorized_account_count if gateway is not None else 0
                ),
                unavailable_authorized_accounts=(
                    gateway.unavailable_authorized_account_count if gateway is not None else 0
                ),
            )

        @app.get(
            "/v1/accounts/{alias}/orders",
            response_model=list[BrokerOrder],
            dependencies=[Depends(authenticate)],
        )
        async def account_orders(alias: str) -> list[BrokerOrder]:
            return await asyncio.to_thread(execution_service.orders, alias)

        @app.get(
            "/v1/accounts/{alias}/positions",
            response_model=list[BrokerPosition],
            dependencies=[Depends(authenticate)],
        )
        async def account_positions(alias: str) -> list[BrokerPosition]:
            return await asyncio.to_thread(execution_service.positions, alias)

    return app
