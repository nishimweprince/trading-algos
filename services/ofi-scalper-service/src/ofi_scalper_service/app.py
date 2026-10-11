"""HTTP surface: health, status, latest features, and the kill switch.

| Route | Auth | |
|---|---|---|
| ``GET /health/live`` | no | process up |
| ``GET /health/ready`` | no | streams connected and every book verified |
| ``GET /v1/status`` | yes | books, risk, gate, recorder, fees, feed latency |
| ``GET /v1/features?symbol=`` | yes | the latest feature vector |
| ``GET /v1/signals`` | yes | recent threshold crossings and what was done |
| ``GET /v1/trades?date=`` | yes | recent cycles, or one UTC day's with its summary |
| ``POST /v1/kill`` | yes | halt: close every shadow cycle, enter nothing; until ack |
| ``POST /v1/kill/ack`` | yes | clear a halt (refused while the kill file exists) |
| ``POST /v1/drill/{name}`` | yes | ``disconnect`` or ``gap_handling`` drill (shadow) |
| ``GET /dashboard`` | page; its calls use the key | signals, cycles, policy state, live |
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from ta_core import ServiceError, create_base_app
from ta_core.logging_config import configure_file_logs, configure_logging, log_event
from ta_notify import Notifier
from ta_plugin_api import MARKET_DATA_GROUP, load_providers

from .alerts import Alerts
from .config import PROVIDER, ExecutionMode, Settings, load_settings
from .dashboard import DASHBOARD_HTML
from .execution_bridge import ExecutionBridge
from .latency import default_latency_path, order_delay_ms
from .model import LoadedModel, load_model
from .recorder import Recorder
from .regime_gate import RegimeGate
from .risk import RiskLimits, RiskState
from .runtime import ScalperRuntime
from .sample_log import SampleLog
from .telegram_commands import TelegramCommands
from .trades import BackgroundAppender, TradeBook, summarise

__all__ = ["LOGGER_NAME", "build_runtime", "create_app"]

LOGGER_NAME = "ofi_scalper_service.events"


class KillRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


def load_pinned_model(settings: Settings) -> LoadedModel | None:
    """The pinned model, verified; None without OFI_MODEL_VERSION. Raises if unusable."""
    if settings.model_version is None:
        return None
    # Mode off may load a non-passing model to watch its features; it trades nothing.
    # OFI_ALLOW_UNGATED lets shadow trade one.
    require_pass = settings.execution_mode is ExecutionMode.SHADOW and not settings.allow_ungated
    return load_model(settings.model_dir, settings.model_version, require_pass=require_pass)


def shadow_fills(settings: Settings) -> dict[str, Any]:
    """The fill model of shadow mode: research's, at 1x latency."""
    delay_ms = settings.shadow_delay_ms
    if delay_ms is None:
        delay_ms = order_delay_ms(default_latency_path())
    return {"queue_model": settings.shadow_queue_model, "delay_ns": int(delay_ms * 1e6)}


def build_bridge(
    settings: Settings,
    *,
    risk: RiskState,
    alerts: Alerts,
    model: LoadedModel | None,
    fees: Any,
) -> ExecutionBridge | None:
    if settings.execution_mode is ExecutionMode.OFF:
        return None
    return ExecutionBridge(
        settings,
        risk=risk,
        alerts=alerts,
        model=model,
        state_dir=settings.state_dir,
        fees=fees,
        # Trade and signal records are written off the decision loop.
        book=TradeBook(settings.state_dir, writer=BackgroundAppender()),
        **shadow_fills(settings),
    )


def build_runtime(settings: Settings, notifier: Any | None) -> ScalperRuntime:
    """Wire the plugin's extras through the discovered factory (never constructors)."""
    factory = load_providers(MARKET_DATA_GROUP, [PROVIDER])[PROVIDER]
    model = load_pinned_model(settings)
    recorder = (
        Recorder(
            settings.record_dir,
            host_tag=settings.host_tag,
            min_free_bytes=int(settings.min_free_disk_gb * 1e9),
        )
        if settings.record_enabled
        else None
    )
    sample_log = SampleLog(settings.sample_log_dir) if settings.sample_log_dir else None
    rest = factory.rest(settings)
    streams = factory.streams(
        settings, rest=rest, on_raw=recorder.write if recorder is not None else None
    )
    account = factory.account(settings, rest=rest)
    # Shadow records loss-limit breaches and keeps trading; only a kill halts.
    risk = RiskState(
        RiskLimits.from_settings(settings),
        state_path=settings.state_dir / "halt.json",
        enforce_loss_limits=False,
    )
    alerts = Alerts(notifier)
    runtime = ScalperRuntime(
        settings,
        streams=streams,
        account=account,
        risk=risk,
        gate=RegimeGate.from_settings(settings),
        alerts=alerts,
        recorder=recorder,
        sample_log=sample_log,
        model=model,
    )
    runtime.bridge = build_bridge(
        settings, risk=risk, alerts=alerts, model=model, fees=runtime.fee_bp
    )
    return runtime


def create_app(settings: Settings | None = None, runtime: ScalperRuntime | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Both log sinks write on threads: the decision loop only enqueues.
    configure_logging(settings.log_level, name=LOGGER_NAME, background=True)
    configure_file_logs(settings.events_log_path, background=True)
    state: dict[str, Any] = {"runtime": runtime}

    async def telegram_handler(command: str, user_id: int) -> str:
        current: ScalperRuntime = state["runtime"]
        if command == "kill":
            fired = current.kill("telegram", f"user {user_id}")
            return "HALTED. Ack via POST /v1/kill/ack." if fired else "Already halted."
        status = current.status()
        risk = status["risk"]
        return (
            f"ready={status['ready']} halted={risk['halted']} "
            f"pauses={risk['pauses']} mode={status['execution_mode']} "
            f"model={(status['model'] or {}).get('version')}"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(timeout=settings.notification_timeout_seconds)
        notifier = Notifier(settings, http, source="ofi-scalper")
        if state["runtime"] is None:
            state["runtime"] = build_runtime(settings, notifier)
        current: ScalperRuntime = state["runtime"]
        log_event("ofi_starting", profile=settings.profile, mode=settings.execution_mode.value)
        await current.start()
        telegram = None
        if settings.telegram_bot_token is not None:
            telegram = TelegramCommands(
                settings.telegram_bot_token, settings.telegram_admin_user_ids, telegram_handler
            )
            current.spawn(telegram.run(), "ofi-telegram")
        current.alerts.send(
            "start",
            "OFI scalper started",
            [
                f"profile={settings.profile} mode={settings.execution_mode.value}",
                f"symbols={','.join(settings.binance_futures_symbols)}",
            ],
        )
        try:
            yield
        finally:
            log_event("ofi_stopping", profile=settings.profile)
            await current.close()
            if telegram is not None:
                await telegram.aclose()
            await http.aclose()

    def readiness() -> tuple[bool, dict[str, Any]]:
        current: ScalperRuntime | None = state["runtime"]
        if current is None:
            return False, {"runtime": "not started"}
        return current.readiness()

    app, authenticate = create_base_app(
        settings, title="ofi-scalper-service", lifespan=lifespan, readiness=readiness
    )
    auth = [Depends(authenticate)]

    def current_runtime() -> ScalperRuntime:
        current = state["runtime"]
        if current is None:
            raise ServiceError(503, "not_started", "The runtime has not started")
        return current

    @app.get("/v1/status", dependencies=auth)
    async def status() -> dict[str, Any]:
        return current_runtime().status()

    @app.get("/v1/features", dependencies=auth)
    async def features(symbol: str = Query(min_length=1, max_length=20)) -> dict[str, Any]:
        current = current_runtime()
        latest = current.latest.get(symbol.upper())
        if latest is None:
            raise ServiceError(404, "no_features", f"No features yet for {symbol.upper()}")
        return latest

    @app.get("/v1/signals", dependencies=auth)
    async def signals(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        bridge = current_runtime().bridge
        items = list(bridge.book.recent_signals)[-limit:] if bridge is not None else []
        return {"signals": items[::-1]}

    @app.get("/v1/trades", dependencies=auth)
    async def trades(
        day: Annotated[date | None, Query(alias="date")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        bridge = current_runtime().bridge
        if bridge is None:
            return {"trades": [], "summary": summarise([])}
        if day is None:
            items = list(bridge.book.recent_trades)[-limit:]
            return {"trades": items[::-1], "summary": summarise(items)}
        items = bridge.book.trades.read(day.isoformat())
        return {
            "date": day.isoformat(),
            "trades": items[::-1][:limit],
            "summary": summarise(items, bridge.book.signals.read(day.isoformat())),
        }

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return DASHBOARD_HTML

    @app.post("/v1/kill", dependencies=auth)
    async def kill(body: KillRequest | None = None) -> dict[str, Any]:
        current = current_runtime()
        fired = current.kill("http", body.reason if body else "")
        return {"halted": True, "fired": fired, "risk": current.risk.snapshot()["halt"]}

    @app.post("/v1/kill/ack", dependencies=auth)
    async def ack() -> dict[str, Any]:
        current = current_runtime()
        ok, message = current.ack("http")
        if not ok:
            raise ServiceError(409, "ack_refused", message)
        return {"halted": current.risk.halted, "message": message}

    @app.post("/v1/drill/{name}", dependencies=auth)
    async def drill(name: str) -> dict[str, Any]:
        current = current_runtime()
        try:
            return await current.run_drill(name)
        except ValueError as exc:
            raise ServiceError(409, "drill_refused", str(exc)) from None

    return app
