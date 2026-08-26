"""The legacy MetaTrader 5 signal surface, kept byte-compatible.

lux-algo, ipda, signals-scrapper and lookup-trader all POST to
``MT5_SIGNAL_API_URL`` → ``/v1/signals`` on ports 8000/8001, and none of them
migrate in this pass. So this module keeps mt5-trader's routes, request and
response shapes exactly as they were, including two things that look like
inconsistencies and are not:

- ``/v1/market-data/candles`` and ``/v1/market-data/tick`` share their paths with
  the cTrader routes but return the ``Legacy*`` shapes (epoch-int ``time``, int
  ``volume``, no provenance). Only one adapter is enabled per host, so the paths
  never actually collide; the shapes differ because they always have.
- Idempotency runs off ``SignalRequest.canonical_json``, whose hash gates replay
  against the existing signals.db. It is carried over untouched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query
from ta_contracts import (
    LegacyCandlesResponse,
    LegacyTickResponse,
    SignalRequest,
    SignalResponse,
    SignalStatus,
    Timeframe,
)
from ta_core import COMMON_ERRORS, ErrorResponse

from .adapters.mt5.legacy_repository import SignalRepository
from .adapters.mt5.market_data_service import MarketDataService
from .adapters.mt5.mt5_adapter import MT5Adapter
from .adapters.mt5.notifications import NotificationClient
from .adapters.mt5.service import SignalExecutionService
from .adapters.mt5.signal_log import SignalFileLog
from .config import Settings
from .logging_config import log_event


@dataclass
class MT5Stack:
    """Everything the MT5 adapter needs, built once and shared with the routes."""

    settings: Settings
    adapter: MT5Adapter
    repository: SignalRepository
    service: SignalExecutionService
    market_data: MarketDataService
    notifications: NotificationClient
    initialized: bool = False


def build_stack(settings: Settings, adapter: MT5Adapter | None = None) -> MT5Stack:
    """Assemble the MT5 stack.

    `RealMT5Adapter` is imported here rather than at module scope so importing
    this module on a non-Windows host stays harmless; the MetaTrader5 package is
    only touched when the adapter is actually constructed.
    """
    if adapter is None:
        from .adapters.mt5.mt5_adapter import RealMT5Adapter

        adapter = RealMT5Adapter()
    repository = SignalRepository(settings.database_path)
    notifications = NotificationClient(settings)
    service = SignalExecutionService(
        settings,
        adapter,
        repository,
        signal_file_log=SignalFileLog(settings.signals_log_path),
        notification_client=notifications,
    )
    return MT5Stack(
        settings=settings,
        adapter=adapter,
        repository=repository,
        service=service,
        market_data=MarketDataService(settings, adapter),
        notifications=notifications,
    )


async def startup(stack: MT5Stack) -> None:
    """Initialise the terminal, then reconcile anything left mid-flight.

    Reconciliation is not optional: a crash between order_send and the ledger
    write leaves a signal that the broker executed and the database calls
    unresolved, and only a history scan can tell the difference.
    """
    settings = stack.settings
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
    await asyncio.to_thread(stack.repository.initialize)
    log_event(
        "audit_database_initialized",
        console=False,
        database_path=str(settings.database_path),
    )
    try:
        log_event("mt5_initialize_started", console=False)
        stack.initialized = await asyncio.to_thread(stack.adapter.initialize, settings)
        log_event("mt5_initialize_completed", console=False, initialized=stack.initialized)
        if stack.initialized:
            await asyncio.to_thread(stack.service.reconcile_startup)
            probe_results = await stack.market_data.probe_symbols()
            symbols_ok = sum(1 for result in probe_results if result.get("ok"))
            log_event(
                "market_data_probe_completed",
                profile=settings.profile,
                symbols_total=len(probe_results),
                symbols_ok=symbols_ok,
                symbols_failed=len(probe_results) - symbols_ok,
                results=probe_results,
            )
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
    if stack.initialized:
        await asyncio.to_thread(stack.adapter.shutdown)
        log_event("mt5_shutdown_completed", console=False)


def register_routes(app: FastAPI, stack: MT5Stack, authenticate: Any) -> None:
    service = stack.service
    market_data = stack.market_data

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

    @app.get(
        "/v1/market-data/candles",
        response_model=LegacyCandlesResponse,
        responses=COMMON_ERRORS,
        dependencies=[Depends(authenticate)],
    )
    async def get_candles(
        quote: str = Query(..., min_length=1, max_length=64),
        timeframe: Timeframe = Query(default=Timeframe.M1),  # noqa: B008
        count: int = Query(default=500, gt=0),
    ) -> LegacyCandlesResponse:
        return await market_data.get_candles(quote, timeframe, count)

    @app.get(
        "/v1/market-data/tick",
        response_model=LegacyTickResponse,
        responses=COMMON_ERRORS,
        dependencies=[Depends(authenticate)],
    )
    async def get_tick(
        quote: str = Query(..., min_length=1, max_length=64),
    ) -> LegacyTickResponse:
        return await market_data.get_tick(quote)
