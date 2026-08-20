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
from pydantic import ValidationError

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
    PerformanceUnit,
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
            engine = ClosedBarEngine(
                settings.session_windows(),
                settings.engine_params(),
                settings.session_anchors(),
            )
            notifier = Notifier(settings, http)
            trader = PaperTrader(settings, store, engine, notifier, settings.paper_state_path)
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
        return CandlesResponse(symbol=symbol, timeframe=timeframe, candles=candles, source=resolved)

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
        window_names = {window.name for window in windows}
        anchors = [anchor for anchor in s.session_anchors() if anchor.name in window_names]
        engine = ClosedBarEngine(windows, params, anchors)
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
            entry_mode=settings.entry_mode,
            tp_mode=settings.tp_mode,
            lock_mode=settings.lock_mode,
            hedge_ratio_initial=settings.hedge_ratio_initial,
            hedge_trigger_mode=settings.hedge_trigger_mode,
            hedge_failure_k=settings.hedge_failure_k,
            hedge_ratio_staged=settings.hedge_ratio_staged,
            oco_buffer_mode=settings.oco_buffer_mode,
            oco_buffer_value=settings.oco_buffer_value,
            oco_expiry_bars=settings.oco_expiry_bars,
            allow_reentry=settings.allow_reentry,
            stop_mode=settings.stop_mode,
            sl_mult=settings.sl_mult,
            fixed_stop_pips=settings.fixed_stop_pips,
            rr=settings.rr,
            min_stop_pips=settings.min_stop_pips,
            qty=settings.qty,
            pip_size=settings.pip_size,
            point_value=settings.point_value,
            orb_minutes=settings.orb_minutes,
            entry_delay_minutes=settings.entry_delay_minutes,
            anchor_tolerance_minutes=settings.anchor_tolerance_minutes,
            intrabar_mode=settings.intrabar_mode,
            performance_unit=settings.performance_unit,
            dollars_per_pip_per_qty=settings.dollars_per_pip_per_qty,
            cost_model=settings.cost_model,
            spread_pips_per_side=settings.spread_pips_per_side,
            slippage_pips_per_side=settings.slippage_pips_per_side,
            commission_pips_per_side=settings.commission_pips_per_side,
            swap_long_pips_per_rollover=settings.swap_long_pips_per_rollover,
            swap_short_pips_per_rollover=settings.swap_short_pips_per_rollover,
            swap_rollover_time=settings.swap_rollover_time,
            swap_timezone=settings.swap_timezone,
            swap_triple_weekday=settings.swap_triple_weekday,
            session_cost_overrides=settings.session_cost_overrides,
            breakeven_cost_report=settings.breakeven_cost_report,
            risk_mode=settings.risk_mode,
            risk_pct_per_r=settings.risk_pct_per_r,
            max_pair_risk_pct=settings.max_pair_risk_pct,
            max_open_risk_pct=settings.max_open_risk_pct,
            max_concurrent_structures=settings.max_concurrent_structures,
            one_open_per_session=settings.one_open_per_session,
            contract_size=settings.contract_size,
            firm_profile=settings.firm_profile,
            firm_initial_balance=(
                settings.firm_initial_balance
                if settings.firm_initial_balance is not None
                else settings.initial_capital
            ),
            firm_daily_loss_limit_pct=settings.firm_daily_loss_limit_pct,
            firm_total_loss_limit_pct=settings.firm_total_loss_limit_pct,
            firm_timezone=settings.firm_timezone,
            firm_daily_reset_time=settings.firm_daily_reset_time,
            firm_breach_action=settings.firm_breach_action,
            time_exit_mode=settings.time_exit_mode,
            max_age_hours=settings.max_age_hours,
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
    if body.entry_mode is not None:
        updates["entry_mode"] = body.entry_mode
    if body.lock_pips is not None:
        updates["lock_pips"] = body.lock_pips
    if body.stop_mode is not None:
        updates["stop_mode"] = body.stop_mode
    if body.sl_mult is not None:
        updates["sl_mult"] = body.sl_mult
    if body.fixed_stop_pips is not None:
        updates["fixed_stop_pips"] = body.fixed_stop_pips
    if body.rr is not None:
        updates["rr"] = body.rr
    if body.min_stop_pips is not None:
        updates["min_stop_pips"] = body.min_stop_pips
    if body.qty is not None:
        updates["qty"] = body.qty
    if body.performance_unit is not None:
        updates["performance_unit"] = body.performance_unit
    if body.orb_minutes is not None:
        updates["orb_minutes"] = body.orb_minutes
    if body.entry_delay_minutes is not None:
        updates["entry_delay_minutes"] = body.entry_delay_minutes
    if body.anchor_tolerance_minutes is not None:
        updates["anchor_tolerance_minutes"] = body.anchor_tolerance_minutes
    if body.intrabar_mode is not None:
        updates["intrabar_mode"] = body.intrabar_mode
    for field in (
        "cost_model",
        "spread_pips_per_side",
        "slippage_pips_per_side",
        "commission_pips_per_side",
        "swap_long_pips_per_rollover",
        "swap_short_pips_per_rollover",
        "swap_rollover_time",
        "swap_timezone",
        "swap_triple_weekday",
        "session_cost_overrides",
        "breakeven_cost_report",
        "risk_mode",
        "risk_pct_per_r",
        "max_pair_risk_pct",
        "max_open_risk_pct",
        "max_concurrent_structures",
        "one_open_per_session",
        "hedge_ratio_initial",
        "hedge_trigger_mode",
        "hedge_failure_k",
        "hedge_ratio_staged",
        "oco_buffer_mode",
        "oco_buffer_value",
        "oco_expiry_bars",
        "allow_reentry",
        "firm_profile",
        "firm_initial_balance",
        "firm_daily_loss_limit_pct",
        "firm_total_loss_limit_pct",
        "firm_timezone",
        "firm_daily_reset_time",
        "time_exit_mode",
        "max_age_hours",
    ):
        value = getattr(body, field)
        if value is not None:
            updates[field] = value
    performance_unit = updates.get("performance_unit", base.performance_unit)
    if performance_unit == PerformanceUnit.DOLLARS and base.dollars_per_pip_per_qty is None:
        raise HTTPException(
            status_code=422,
            detail="Dollar performance requires DOLLARS_PER_PIP_PER_QTY configuration",
        )
    try:
        # model_copy skips validators, which would let an override break a cross-field rule
        # (fixed stop with no distance, ORB not a multiple of the bar) and fail silently later.
        return EngineParams.model_validate(base.model_dump() | updates)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from exc


async def _paper_loop(trader: PaperTrader, interval: float) -> None:
    while True:
        try:
            await trader.tick()
        except Exception:  # noqa: BLE001
            log_event("paper_tick_failed", level=logging.ERROR, exc_info=True)
        await asyncio.sleep(interval)
