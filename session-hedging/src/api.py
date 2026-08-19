from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from candles import CandleStore
from config import Settings
from engine import ClosedBarEngine
from logging_config import log_event
from models import (
    TIMEFRAME_MINUTES,
    BacktestReport,
    BacktestRequest,
    CandlesResponse,
    EngineParams,
    PaperStatus,
    ServiceConfig,
    Timeframe,
)
from notifier import Notifier
from paper import PaperTrader
from sessions import build_windows

CLIENT_DIST = Path(__file__).resolve().parent.parent / "client" / "dist"


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            engine = ClosedBarEngine(settings.session_windows(), settings.engine_params())
            notifier = Notifier(settings, http)
            trader = PaperTrader(
                settings, store, engine, notifier, settings.paper_state_path
            )
            trader.load()
            app.state.settings = settings
            app.state.http = http
            app.state.store = store
            app.state.paper = trader
            task: asyncio.Task[None] | None = None
            if settings.paper_enabled:
                task = asyncio.create_task(_paper_loop(trader, settings.poll_interval_seconds))
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app = FastAPI(title="session-hedging", version="0.1.0", lifespan=lifespan)

    async def authenticate(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = settings.api_key
        if expected is None or not expected.get_secret_value():
            return
        secret = expected.get_secret_value()
        if x_api_key is None or not hmac.compare_digest(x_api_key, secret):
            log_event(
                "authentication_failed",
                level=logging.WARNING,
                path=str(request.url.path),
            )
            raise HTTPException(status_code=401, detail="A valid X-API-Key header is required")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        store: CandleStore = request.app.state.store
        ok, detail = await store.gateway_ready()
        if ok:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable", "detail": detail}, status_code=503)

    @app.get("/v1/candles", response_model=CandlesResponse, dependencies=[Depends(authenticate)])
    async def get_candles(
        request: Request,
        symbol: str = Query(..., min_length=1),
        timeframe: Timeframe = Query(default=Timeframe.M15),  # noqa: B008
        count: int = Query(default=500, gt=0),
        to: datetime | None = Query(default=None),  # noqa: B008
        source: str | None = Query(default=None),
    ) -> CandlesResponse:
        store: CandleStore = request.app.state.store
        resolved = _resolve_source(store, symbol, timeframe, source)
        if resolved == "local":
            candles = store.load_local(symbol, timeframe, date_to=to, count=count)
        else:
            candles = await store.fetch_ctrader(symbol, timeframe, count=count, to=to)
        return CandlesResponse(
            symbol=symbol, timeframe=timeframe, candles=candles, source=resolved
        )

    @app.post("/v1/backtests", response_model=BacktestReport, dependencies=[Depends(authenticate)])
    async def run_backtest(request: Request, body: BacktestRequest) -> BacktestReport:
        s: Settings = request.app.state.settings
        store: CandleStore = request.app.state.store
        timeframe = body.timeframe
        symbol = body.symbol
        resolved = _resolve_source(store, symbol, timeframe, body.source)
        if resolved == "local":
            candles = store.load_local(
                symbol, timeframe, date_from=body.date_from, date_to=body.date_to
            )
        else:
            candles = await store.fetch_range(
                symbol, timeframe, date_from=body.date_from, date_to=body.date_to
            )
        if not candles:
            raise HTTPException(status_code=404, detail="No candles for that range and source")
        sessions = body.sessions if body.sessions is not None else s.trading_sessions
        windows = build_windows(sessions, s.session_specs)
        params = _params_from(s, body, timeframe)
        engine = ClosedBarEngine(windows, params)
        engine.run(candles)
        report = engine.report(symbol, timeframe, resolved)
        return report.model_copy(update={"bar_count": len(candles)})

    @app.get("/v1/config", response_model=ServiceConfig, dependencies=[Depends(authenticate)])
    async def service_config() -> ServiceConfig:
        return ServiceConfig(
            symbol=settings.symbol,
            timeframe=settings.timeframe,
            sessions=settings.trading_sessions,
            lock_pips=settings.lock_pips,
            sl_mult=settings.sl_mult,
            rr=settings.rr,
            min_stop_pips=settings.min_stop_pips,
            qty=settings.qty,
            pip_size=settings.pip_size,
        )

    @app.get("/v1/paper", response_model=PaperStatus, dependencies=[Depends(authenticate)])
    async def paper_status(request: Request) -> PaperStatus:
        trader: PaperTrader = request.app.state.paper
        return trader.status()

    if (CLIENT_DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=CLIENT_DIST, html=True), name="ui")

    return app


def _resolve_source(
    store: CandleStore, symbol: str, timeframe: Timeframe, requested: str | None
) -> str:
    if requested == "local":
        return "local"
    if requested == "ctrader":
        return "ctrader"
    return "local" if store.local_exists(symbol, timeframe) else "ctrader"


def _params_from(settings: Settings, body: BacktestRequest, timeframe: Timeframe) -> EngineParams:
    base = settings.engine_params()
    updates: dict[str, object] = {"timeframe_minutes": TIMEFRAME_MINUTES[timeframe]}
    if body.lock_pips is not None:
        updates["lock_pips"] = body.lock_pips
    if body.sl_mult is not None:
        updates["sl_mult"] = body.sl_mult
    if body.rr is not None:
        updates["rr"] = body.rr
    if body.min_stop_pips is not None:
        updates["min_stop_pips"] = body.min_stop_pips
    if body.qty is not None:
        updates["qty"] = body.qty
    return base.model_copy(update=updates)


async def _paper_loop(trader: PaperTrader, interval: float) -> None:
    while True:
        try:
            await trader.tick()
        except Exception:  # noqa: BLE001
            log_event("paper_tick_failed", level=logging.ERROR, exc_info=True)
        await asyncio.sleep(interval)
