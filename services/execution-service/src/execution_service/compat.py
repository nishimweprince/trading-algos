"""The legacy MetaTrader 5 signal surface, kept byte-compatible.

ipda, signals-scrapper and lookup-trader all POST to ``MT5_SIGNAL_API_URL`` →
``/v1/signals`` on ports 8000/8001. So this module keeps mt5-trader's signal and
OCO routes, request and response shapes exactly as they were. Signals are
recorded on the shared execution ledger (see ``signals.py``); idempotency still
runs off ``SignalRequest.canonical_json``, so history imported from signals.db
replays exactly as before.

Its candle and tick routes are gone: MT5 market data is served by
market-data-service, one process per terminal.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query
from ta_contracts import (
    SignalRequest,
    SignalResponse,
    SignalStatus,
)
from ta_core import COMMON_ERRORS, ErrorResponse
from ta_plugin_api import EXECUTION_GROUP, load_providers
from ta_plugin_mt5.execution import MT5Execution
from ta_plugin_mt5.terminal import MT5Adapter
from ta_store import ExecutionRepository

from .adapters.mt5.notifications import NotificationClient
from .adapters.mt5.oco_models import OcoGroupRequest
from .adapters.mt5.oco_repository import OcoRepository
from .adapters.mt5.oco_service import Mt5OcoService
from .adapters.mt5.signal_log import SignalFileLog
from .config import Settings
from .logging_config import log_event
from .migration import migrate_legacy_ledger
from .signals import SignalService


@dataclass
class MT5Stack:
    """Everything the MT5 host needs, built once and shared with the routes."""

    settings: Settings
    provider: MT5Execution
    repository: ExecutionRepository
    service: SignalService
    notifications: NotificationClient
    oco: Mt5OcoService
    initialized: bool = False
    oco_task: asyncio.Task[None] | None = None

    @property
    def adapter(self) -> MT5Adapter:
        return self.provider.adapter


def build_stack(
    settings: Settings,
    repository: ExecutionRepository,
    adapter: MT5Adapter | None = None,
) -> MT5Stack:
    """Assemble the MT5 stack on the shared execution ledger.

    The real terminal comes from the discovered ``mt5`` provider, which imports
    the MetaTrader5 package only when it constructs one, so importing this
    module on a non-Windows host stays harmless.
    """
    provider = load_providers(EXECUTION_GROUP, ["mt5"])["mt5"].execution(
        settings,
        terminal=adapter,
        log=functools.partial(log_event, console=False),
    )
    notifications = NotificationClient(settings)
    service = SignalService(
        settings,
        provider,
        repository,
        signal_file_log=SignalFileLog(settings.signals_log_path),
        notification_client=notifications,
    )
    return MT5Stack(
        settings=settings,
        provider=provider,
        repository=repository,
        service=service,
        notifications=notifications,
        oco=Mt5OcoService(
            service, OcoRepository(settings.database_path.with_suffix(".oco.sqlite3"))
        ),
    )


async def startup(stack: MT5Stack) -> None:
    """Import the legacy ledger, attach the terminal, then reconcile.

    The import runs before anything can execute, so a signal ID that
    signals.db already holds replays instead of reaching the terminal again.
    Reconciliation is not optional either: a crash between order_send and the
    ledger write leaves a signal that the broker executed and the database
    calls unresolved, and only a history scan can tell the difference.
    """
    settings = stack.settings
    log_event(
        "service_starting",
        profile=settings.profile,
        account=stack.provider.account,
        terminal_path=str(settings.terminal_path),
        expected_login=settings.login,
        server=settings.server,
        execution_database_path=str(settings.execution_database_path),
        allowed_symbols=sorted(settings.allowed_symbols),
        allowed_signal_sources=sorted(settings.allowed_signal_sources),
        maximum_volume=str(settings.maximum_volume),
        magic_number=settings.magic_number,
        trading_enabled=settings.trading_enabled,
    )
    await asyncio.to_thread(stack.oco.repository.initialize)
    summary = await asyncio.to_thread(
        migrate_legacy_ledger, settings.database_path, stack.repository, stack.provider.account
    )
    log_event(
        "legacy_ledger_migration",
        level=logging.INFO if not summary.get("missing") else logging.DEBUG,
        console=bool(summary.get("imported")),
        **summary,
    )
    try:
        log_event("mt5_initialize_started", console=False)
        await stack.provider.start()
        stack.initialized = stack.provider.initialized
        log_event("mt5_initialize_completed", console=False, initialized=stack.initialized)
        if stack.initialized:
            await asyncio.to_thread(stack.service.reconcile_startup)
            await stack.provider.reconcile()
            if settings.mt5_oco_enabled or await asyncio.to_thread(stack.oco.repository.all):
                await stack.oco.monitor_once(startup=True)
                stack.oco_task = asyncio.create_task(stack.oco.run())
    except Exception as exc:  # noqa: BLE001 - startup must not crash-loop the host
        stack.initialized = False
        log_event(
            "mt5_initialize_failed",
            level=logging.ERROR,
            console=False,
            exc_info=True,
            reason=type(exc).__name__,
        )


async def shutdown(stack: MT5Stack) -> None:
    log_event("service_stopping", mt5_initialized=stack.initialized)
    if stack.oco_task is not None:
        stack.oco_task.cancel()
        try:
            await stack.oco_task
        except asyncio.CancelledError:
            pass
    if stack.initialized:
        await stack.provider.close()
        log_event("mt5_shutdown_completed", console=False)


def register_routes(app: FastAPI, stack: MT5Stack, authenticate: Any) -> None:
    service = stack.service

    @app.get("/v1/mt5/capabilities", dependencies=[Depends(authenticate)])
    async def mt5_capabilities(
        symbol: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        return await stack.oco.capabilities(symbol)

    @app.get("/v1/mt5/inventory", dependencies=[Depends(authenticate)])
    async def mt5_inventory() -> dict[str, Any]:
        return await stack.oco.inventory()

    @app.post("/v1/mt5/oco", dependencies=[Depends(authenticate)])
    async def submit_oco(request: OcoGroupRequest) -> dict[str, Any]:
        return await stack.oco.submit(request)

    @app.get("/v1/mt5/oco/{group_id}", dependencies=[Depends(authenticate)])
    async def get_oco(group_id: UUID) -> dict[str, Any]:
        return await stack.oco.get(group_id)

    @app.post("/v1/mt5/oco/{group_id}/cancel", dependencies=[Depends(authenticate)])
    async def cancel_oco(
        group_id: UUID,
        reason: str = Query(default="engine_expiry", min_length=1, max_length=120),
    ) -> dict[str, Any]:
        return await stack.oco.cancel(group_id, reason)

    @app.post("/v1/mt5/oco/{group_id}/close", dependencies=[Depends(authenticate)])
    async def close_oco(group_id: UUID) -> dict[str, Any]:
        return await stack.oco.close_owned_group(group_id)

    @app.post("/v1/mt5/oco/{group_id}/acknowledge", dependencies=[Depends(authenticate)])
    async def acknowledge_oco(group_id: UUID) -> dict[str, Any]:
        return await stack.oco.acknowledge_recovery(group_id)

    @app.post(
        "/v1/signals",
        response_model=SignalResponse,
        responses=COMMON_ERRORS,
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
