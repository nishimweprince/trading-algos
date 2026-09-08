from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from ta_clients import CandleStore
from ta_contracts import TIMEFRAME_MINUTES

from . import registry
from .anchors import SessionAnchor
from .comparison import compare_entry_modes
from .config import Settings
from .engine import ClosedBarEngine
from .execution import ExecutionClient
from .execution_bridge import ExecutionBridge
from .harness.fingerprint import candle_sha256
from .logging_config import log_event
from .models import (
    DEFAULT_DOLLARS_PER_PIP_PER_QTY,
    BacktestReport,
    BacktestRequest,
    BrokerOrderView,
    BrokerPositionView,
    Candle,
    CandlesResponse,
    EngineParams,
    EntryModeComparisonReport,
    ExecutionDivergence,
    ExecutionMode,
    ExecutionStatus,
    PaperStatus,
    S7ResearchArtifact,
    ServiceConfig,
    Timeframe,
    TrackedOrderView,
)
from .notifier import Notifier
from .paper import PaperTrader
from .research.s7_artifact import DEFAULT_S7_PATH, load_s7_research_artifact
from .sessions import SessionWindow, build_windows
from .strategies.facade import StrategyExecution

CLIENT_DIST = Path(__file__).resolve().parent.parent / "client" / "dist"


def _sync_backtest(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    strategy: StrategyExecution,
) -> BacktestReport:
    engine = ClosedBarEngine(windows, params, anchors, collect_equity_curve=True, strategy=strategy)
    engine.run(candles)
    return engine.report(symbol, timeframe, source).model_copy(
        update={"bar_count": len(candles), "candle_set_sha256": candle_sha256(candles)}
    )


def _sync_compare(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    strategy: StrategyExecution,
) -> EntryModeComparisonReport:
    return compare_entry_modes(
        candles,
        windows,
        params,
        anchors,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        source=source,
    )


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            engine = ClosedBarEngine(
                settings.session_windows(),
                settings.engine_params(),
                settings.session_anchors(),
                collect_equity_curve=True,
            )
            notifier = Notifier(settings, http)
            execution: ExecutionClient | None = None
            bridge: ExecutionBridge | None = None
            if settings.market_execution_mode.builds_payloads:
                execution = ExecutionClient(settings, http)
                bridge = ExecutionBridge(settings, execution)
            trader = PaperTrader(
                settings, store, engine, notifier, settings.paper_state_path, bridge=bridge
            )
            trader.load()
            if bridge is not None:
                await bridge.reconcile()
            app.state.settings = settings
            app.state.http = http
            app.state.store = store
            app.state.paper = trader
            app.state.execution = execution
            app.state.bridge = bridge
            _log_resolved_configuration(settings)
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

    async def authenticate_strict(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        """Auth for routes that can see or affect real orders.

        ``authenticate`` deliberately fails open when no ``API_KEY`` is configured, which is
        fine for a read-only backtest API. Anything touching execution must not inherit that:
        an unconfigured key is a refusal here, not a waiver.
        """
        expected = settings.api_key
        if expected is None or not expected.get_secret_value():
            log_event(
                "execution_route_unconfigured",
                level=logging.ERROR,
                path=str(request.url.path),
            )
            raise HTTPException(
                status_code=503,
                detail="API_KEY must be configured before execution routes are available",
            )
        await authenticate(request, x_api_key)

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
        params, strategy = _resolve_strategy(s, body, timeframe)
        window_names = {window.name for window in windows}
        anchors = [anchor for anchor in s.session_anchors() if anchor.name in window_names]
        return await asyncio.to_thread(
            _sync_backtest,
            candles,
            windows,
            params,
            anchors,
            symbol=symbol,
            timeframe=timeframe,
            source=resolved,
            strategy=strategy,
        )

    @app.post(
        "/v1/backtests/compare",
        response_model=EntryModeComparisonReport,
        dependencies=[Depends(authenticate)],
    )
    async def compare_backtests(
        request: Request, body: BacktestRequest
    ) -> EntryModeComparisonReport:
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
        params, strategy = _resolve_strategy(s, body, timeframe)
        if params.strategy != registry.DEFAULT_STRATEGY:
            raise HTTPException(
                status_code=422,
                detail=(
                    "compare covers entry modes, not strategies: run one backtest "
                    "per strategy over the same range and match candle_set_sha256"
                ),
            )
        window_names = {window.name for window in windows}
        anchors = [anchor for anchor in s.session_anchors() if anchor.name in window_names]
        return await asyncio.to_thread(
            _sync_compare,
            candles,
            windows,
            params,
            anchors,
            symbol=symbol,
            timeframe=timeframe,
            source=resolved,
            strategy=strategy,
        )

    @app.get("/v1/config", response_model=ServiceConfig, dependencies=[Depends(authenticate)])
    async def service_config() -> ServiceConfig:
        return ServiceConfig(
            symbol=settings.symbol,
            timeframe=settings.timeframe,
            sessions=settings.trading_sessions,
            lock_pips=settings.lock_pips,
            entry_mode=settings.entry_mode,
            tp_mode=settings.tp_mode,
            partial_tp_r=settings.partial_tp_r,
            partial_fraction=settings.partial_fraction,
            lock_mode=settings.lock_mode,
            lock_r=settings.lock_r,
            be_trigger_r=settings.be_trigger_r,
            survivor_exit_mode=settings.survivor_exit_mode,
            survivor_trail_activation_r=settings.survivor_trail_activation_r,
            survivor_trail_gap_r=settings.survivor_trail_gap_r,
            hedge_path_mode=settings.hedge_path_mode,
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
            min_stop_cost_mult=settings.min_stop_cost_mult,
            filter_d1_ema50=settings.filter_d1_ema50,
            filter_nr7=settings.filter_nr7,
            filter_orb_atr_min=settings.filter_orb_atr_min,
            filter_orb_atr_max=settings.filter_orb_atr_max,
            qty=settings.qty,
            pip_size=settings.pip_size,
            point_value=settings.point_value,
            orb_minutes=settings.orb_minutes,
            entry_delay_minutes=settings.entry_delay_minutes,
            anchor_tolerance_minutes=settings.anchor_tolerance_minutes,
            intrabar_mode=settings.intrabar_mode,
            default_dollars_per_pip_per_qty=DEFAULT_DOLLARS_PER_PIP_PER_QTY,
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
            strategies=sorted(registry.available()),
        )

    @app.get("/v1/paper", response_model=PaperStatus, dependencies=[Depends(authenticate)])
    async def paper_status(request: Request) -> PaperStatus:
        trader: PaperTrader = request.app.state.paper
        return trader.status()

    @app.get(
        "/v1/execution",
        response_model=ExecutionStatus,
        dependencies=[Depends(authenticate_strict)],
    )
    async def execution_status(request: Request) -> ExecutionStatus:
        """Engine intent next to broker reality, plus the gap between them."""
        bridge: ExecutionBridge | None = request.app.state.bridge
        client: ExecutionClient | None = request.app.state.execution
        settings_ = request.app.state.settings
        if bridge is None or client is None:
            return ExecutionStatus(
                mode=ExecutionMode.OFF,
                sends_broker_orders=False,
                account=settings_.execution_account,
                volume_lots=settings_.execution_volume_lots,
                gateway_reason="execution is off",
            )

        tracked = [TrackedOrderView(**asdict(order)) for order in bridge.tracked()]
        ready, reason = await client.trading_ready()
        broker_orders: list[dict[str, object]] = []
        broker_positions: list[dict[str, object]] = []
        if bridge.mode.sends_orders and ready:
            broker_orders = await client.list_orders()
            broker_positions = await client.list_positions()

        trader: PaperTrader = request.app.state.paper
        return ExecutionStatus(
            mode=bridge.mode,
            sends_broker_orders=bridge.mode.sends_orders,
            account=client.account,
            volume_lots=settings_.execution_volume_lots,
            halted_reason=bridge.halted_reason,
            consecutive_failures=bridge.consecutive_failures,
            gateway_ready=ready,
            gateway_reason=reason,
            tracked_orders=tracked,
            broker_orders=[BrokerOrderView(**item) for item in broker_orders],
            broker_positions=[BrokerPositionView(**item) for item in broker_positions],
            divergence=_divergence(
                bridge=bridge,
                engine=trader.engine,
                broker_orders=broker_orders,
                broker_positions=broker_positions,
                pip_size=settings_.pip_size,
            ),
        )

    @app.get(
        "/v1/research/s7-propguard-monte-carlo",
        response_model=S7ResearchArtifact,
        dependencies=[Depends(authenticate)],
    )
    async def s7_research_artifact() -> S7ResearchArtifact:
        """Read-only S7 research simulation panel. Not interactive-backtest or broker facts."""
        path = DEFAULT_S7_PATH
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail="S7 research artifact is not present; this is not an interactive backtest",
            )
        return load_s7_research_artifact(path)

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


def _resolve_strategy(
    settings: Settings, body: BacktestRequest, timeframe: Timeframe
) -> tuple[EngineParams, StrategyExecution]:
    """Resolve the strategy once, into the parameters and the staging it runs with.

    Both halves come from the same plugin, so a request cannot end up running
    one strategy's parameters through another's staging -- which is what
    happened while the engine imported the built-in by name.

    The name resolves to session_hedge when unset, so existing callers are
    unaffected; an unknown name is a 422 rather than a silent fall-back to the
    default. A request whose overrides break a cross-field EngineParams rule is
    also a 422. The compare endpoint shares this path, so it rejects unknown
    strategies too instead of silently running the default.
    """
    try:
        plugin = registry.get(body.strategy)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        params = plugin.build(
            registry.StrategyBuildInputs(
                base=settings.engine_params(),
                body=body,
                timeframe_minutes=TIMEFRAME_MINUTES[timeframe],
            )
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from exc
    return params, registry.execution_for(plugin)


def _divergence(
    *,
    bridge: ExecutionBridge,
    engine: ClosedBarEngine,
    broker_orders: list[dict[str, object]],
    broker_positions: list[dict[str, object]],
    pip_size: float,
) -> ExecutionDivergence:
    """Compare what the engine thinks it holds with what the broker reports.

    In shadow mode the broker lists are empty by construction, so a mismatch there is
    expected rather than alarming and is called out in the notes.
    """
    engine_open = [pair for pair in engine.pairs if pair.long_open or pair.short_open]
    resting = bridge.resting_orders()
    filled = [order for order in bridge.tracked() if order.position_id is not None]

    broker_position_ids = {
        int(item["position_id"])
        for item in broker_positions
        if isinstance(item.get("position_id"), int)
    }
    matched_position_ids = {order.position_id for order in filled if order.position_id is not None}

    slippage: list[float] = []
    for order in filled:
        if order.fill_price is not None and order.entry_price is not None and pip_size > 0:
            slippage.append((order.fill_price - order.entry_price) / pip_size)

    notes: list[str] = []
    if not bridge.mode.sends_orders:
        notes.append("shadow mode: payloads are recorded, nothing was sent to the broker")
    if bridge.halted_reason:
        notes.append(f"execution halted: {bridge.halted_reason}")

    return ExecutionDivergence(
        engine_open_structures=len(engine_open),
        broker_open_positions=len(broker_positions),
        engine_resting_orders=len(resting),
        broker_resting_orders=len(broker_orders),
        positions_matched=(
            len(engine_open) == len(broker_positions) if bridge.mode.sends_orders else True
        ),
        orders_matched=(len(resting) == len(broker_orders) if bridge.mode.sends_orders else True),
        unmatched_broker_positions=sorted(broker_position_ids - matched_position_ids),
        unmatched_engine_orders=[order.pair_id for order in resting if order.order_id is None],
        slippage_pips=slippage,
        mean_slippage_pips=(sum(slippage) / len(slippage)) if slippage else None,
        notes=notes,
    )


def _log_resolved_configuration(settings: Settings) -> None:
    """Say what is actually running, once, at startup.

    The UI sends strategy parameters per request while this loop reads only the
    environment, so the two can silently disagree. Printing the resolved surface makes a
    mismatch visible in the journal instead of in the fills.
    """
    params = settings.engine_params()
    log_event(
        "resolved_configuration",
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        entry_mode=params.entry_mode.value,
        oco_buffer_mode=params.oco_buffer_mode.value,
        oco_buffer_value=params.oco_buffer_value,
        oco_expiry_bars=params.oco_expiry_bars,
        stop_mode=params.stop_mode.value,
        sl_mult=params.sl_mult,
        rr=params.rr,
        lock_pips=params.lock_pips,
        be_trigger_r=params.be_trigger_r,
        entry_hours_utc_exclude=params.entry_hours_utc_exclude,
        sessions=[window.name for window in settings.session_windows()],
        execution_mode=settings.market_execution_mode.value,
        execution_account=settings.execution_account or None,
        execution_volume_lots=settings.execution_volume_lots,
    )


async def _paper_loop(trader: PaperTrader, interval: float) -> None:
    while True:
        try:
            await trader.tick()
        except Exception:  # noqa: BLE001
            log_event("paper_tick_failed", level=logging.ERROR, exc_info=True)
        await asyncio.sleep(interval)
