"""HTTP surface: health, status, latest features, and the kill switch.

| Route | Auth | |
|---|---|---|
| ``GET /health/live`` | no | process up |
| ``GET /health/ready`` | no | streams connected and every book verified |
| ``GET /v1/status`` | yes | books, risk, gate, recorder, fees, feed latency |
| ``GET /v1/features?symbol=`` | yes | the latest feature vector |
| ``POST /v1/kill`` | yes | halt: (would) cancel all, flatten; until ack |
| ``POST /v1/kill/ack`` | yes | clear a halt (refused while the kill file exists) |
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from ta_core import ServiceError, create_base_app
from ta_core.logging_config import configure_file_logs, configure_logging, log_event
from ta_notify import Notifier
from ta_plugin_api import MARKET_DATA_GROUP, load_providers

from .alerts import Alerts
from .config import PROVIDER, Settings, load_settings
from .recorder import Recorder
from .regime_gate import RegimeGate
from .risk import RiskLimits, RiskState
from .runtime import ScalperRuntime
from .telegram_commands import TelegramCommands

__all__ = ["LOGGER_NAME", "build_runtime", "create_app"]

LOGGER_NAME = "ofi_scalper_service.events"


class KillRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


def build_runtime(settings: Settings, notifier: Any | None) -> ScalperRuntime:
    """Wire the plugin's extras through the discovered factory (never constructors)."""
    factory = load_providers(MARKET_DATA_GROUP, [PROVIDER])[PROVIDER]
    recorder = (
        Recorder(settings.record_dir, host_tag=settings.host_tag)
        if settings.record_enabled
        else None
    )
    rest = factory.rest(settings)
    streams = factory.streams(
        settings, rest=rest, on_raw=recorder.write if recorder is not None else None
    )
    account = factory.account(settings, rest=rest)
    return ScalperRuntime(
        settings,
        streams=streams,
        account=account,
        risk=RiskState(RiskLimits.from_settings(settings)),
        gate=RegimeGate.from_settings(settings),
        alerts=Alerts(notifier),
        recorder=recorder,
    )


def create_app(settings: Settings | None = None, runtime: ScalperRuntime | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, name=LOGGER_NAME)
    configure_file_logs(settings.events_log_path)
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
            f"pauses={risk['pauses']} mode={status['execution_mode']}"
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

    return app
