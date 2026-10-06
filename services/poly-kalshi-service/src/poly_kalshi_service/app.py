"""HTTP surface: health, status, decisions, and the kill switch.

| Route | Auth | |
|---|---|---|
| ``GET /health/live`` | no | process up |
| ``GET /health/ready`` | no | both feeds connected and every asset's window identified |
| ``GET /v1/status`` | yes | feeds, recorder, per-asset window and quotes, ledger, risk |
| ``GET /v1/recent`` | yes | the last decisions, fills and settlements |
| ``GET /v1/series?since=`` | yes | per-second price points per asset (two windows deep) |
| ``POST /v1/kill`` | yes | halt new paper entries (recording goes on) until ack |
| ``POST /v1/kill/ack`` | yes | clear a halt (refused while the kill file exists) |
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from ta_core import ServiceError, create_base_app
from ta_core.logging_config import configure_file_logs, configure_logging, log_event
from ta_notify import Notifier

from .alerts import Alerts
from .build import build_id, config_hash
from .config import ExecutionMode, Settings, load_settings
from .engine import Engine
from .feeds import KalshiFeed, KalshiSigner, PolyFeed
from .ledger import Ledger
from .recorder import Recorder
from .risk import RiskLimits, RiskState
from .runtime import Runtime
from .telegram_commands import TelegramCommands
from .trades import JsonlDaily
from .venue import PaperVenue
from .windows import Discovery

__all__ = ["LOGGER_NAME", "build_runtime", "create_app", "is_loopback"]

LOGGER_NAME = "poly_kalshi_service.events"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class KillRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


def build_runtime(settings: Settings, notifier: Any | None, http: httpx.AsyncClient) -> Runtime:
    alerts = Alerts(notifier)
    ledger = Ledger.open(
        settings.state_dir / "ledger.json",
        settings.paper_start_cash_usd,
        epoch=settings.results_epoch,
    )
    risk = RiskState(
        RiskLimits(
            max_open_notional_usd=settings.max_open_notional_usd,
            daily_loss_limit_usd=settings.daily_loss_limit_usd,
            kill_file=settings.kill_file_path,
        )
    )
    venue = (
        PaperVenue(settings.paper_latency_ms)
        if settings.execution_mode is ExecutionMode.PAPER
        else None
    )
    engine = Engine(
        settings.params,
        settings.assets,
        ledger=ledger,
        risk=risk,
        venue=venue,
        journal=JsonlDaily(settings.state_dir / "journal"),
        alert=alerts.send,
        stamp={
            "epoch": settings.results_epoch,
            "build": build_id(),
            "config": config_hash(settings.params, settings.paper_latency_ms),
        },
    )
    recorder = (
        Recorder(
            settings.record_dir,
            host_tag=settings.host_tag,
            min_free_bytes=int(settings.min_free_disk_gb * 1e9),
        )
        if settings.record_enabled
        else None
    )
    runtime: Runtime | None = None

    def emit(venue_tag: str, text: str) -> None:
        assert runtime is not None
        runtime.ingest(venue_tag, text)

    signer = KalshiSigner(
        settings.kalshi_api_key_id, settings.kalshi_private_key_path.expanduser().read_bytes()
    )
    runtime = Runtime(
        settings,
        engine=engine,
        discovery=Discovery(
            http, gamma_url=settings.poly_gamma_url, kalshi_rest_url=settings.kalshi_rest_url
        ),
        kalshi=KalshiFeed(settings.kalshi_ws_url, emit, signer, max_queue=settings.ws_max_queue),
        poly=PolyFeed(settings.poly_ws_url, emit, max_queue=settings.ws_max_queue),
        alerts=alerts,
        recorder=recorder,
        http=http,
    )
    return runtime


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, name=LOGGER_NAME)
    configure_file_logs(settings.events_log_path)
    state: dict[str, Any] = {"runtime": runtime}

    async def telegram_handler(command: str, user_id: int) -> str:
        current: Runtime = state["runtime"]
        if command == "kill":
            fired = current.kill("telegram", f"user {user_id}")
            return "HALTED. Ack via POST /v1/kill/ack." if fired else "Already halted."
        engine = current.engine.status()
        ledger = engine["ledger"] or {}
        return (
            f"mode={settings.execution_mode.value} halted={engine['risk']['halted']} "
            f"cash={ledger.get('cash')} open={ledger.get('open_positions')} "
            f"today={ledger.get('realised_today')} total={ledger.get('realised_total')}"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(timeout=10.0)
        notifier = Notifier(settings, http, source="poly-kalshi")
        if state["runtime"] is None:
            state["runtime"] = build_runtime(settings, notifier, http)
        current: Runtime = state["runtime"]
        log_event("pk_starting", profile=settings.profile, mode=settings.execution_mode.value)
        if not is_loopback(settings.host):
            # The kill switch lives on this API: it should be reachable only
            # from the host (SSH tunnel), never from the internet.
            log_event("api_exposed", level=logging.ERROR, host=settings.host, port=settings.port)
            current.alerts.send(
                "api_exposed",
                "poly-kalshi: API bound to a non-loopback address",
                [
                    f"HOST={settings.host} port {settings.port}: reachable beyond this host.",
                    "Set HOST=127.0.0.1 and reach it over SSH; check the firewall too.",
                ],
                always=True,
            )
        await current.start()
        telegram = None
        if settings.telegram_bot_token is not None:
            telegram = TelegramCommands(
                settings.telegram_bot_token,
                settings.telegram_admin_user_ids,
                telegram_handler,
                on_conflict=lambda error: current.alerts.send(
                    "telegram_conflict",
                    "poly-kalshi: Telegram /kill is not reaching this service",
                    [
                        "Another process polls the same bot token (getUpdates Conflict).",
                        "Give PK_TELEGRAM_BOT_TOKEN a bot of its own; until then use "
                        "POST /v1/kill or the kill file.",
                    ],
                    always=True,
                ),
            )
            current.telegram = telegram
            current.spawn(telegram.run(), "pk-telegram")
        current.alerts.send(
            "start",
            "poly-kalshi started",
            [
                f"profile={settings.profile} mode={settings.execution_mode.value}",
                f"assets={','.join(settings.assets)} host={settings.host_tag}",
            ],
        )
        try:
            yield
        finally:
            log_event("pk_stopping", profile=settings.profile)
            await current.close()
            if telegram is not None:
                await telegram.aclose()
            await http.aclose()

    def readiness() -> tuple[bool, dict[str, Any]]:
        current: Runtime | None = state["runtime"]
        if current is None:
            return False, {"runtime": "not started"}
        return current.ready()

    app, authenticate = create_base_app(
        settings, title="poly-kalshi-service", lifespan=lifespan, readiness=readiness
    )
    auth = [Depends(authenticate)]

    def current_runtime() -> Runtime:
        current = state["runtime"]
        if current is None:
            raise ServiceError(503, "not_started", "The runtime has not started")
        return current

    @app.get("/v1/status", dependencies=auth)
    async def status() -> dict[str, Any]:
        return current_runtime().status()

    @app.get("/v1/recent", dependencies=auth)
    async def recent(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
        return {"recent": list(current_runtime().engine.recent)[-limit:][::-1]}

    @app.get("/v1/series", dependencies=auth)
    async def series(since: int = Query(default=0, ge=0)) -> dict[str, Any]:
        """Per-second price points after ``since`` (unix s): ``[sec, open_unix,
        kalshi_mid, poly_yes_mid, yes_bid, yes_ask, no_bid, no_ask]``."""
        return {"points": current_runtime().engine.series_since(since)}

    @app.post("/v1/kill", dependencies=auth)
    async def kill(body: KillRequest | None = None) -> dict[str, Any]:
        current = current_runtime()
        fired = current.kill("http", body.reason if body else "")
        return {"halted": True, "fired": fired}

    @app.post("/v1/kill/ack", dependencies=auth)
    async def ack() -> dict[str, Any]:
        ok, message = current_runtime().engine.risk.ack()
        if not ok:
            raise ServiceError(409, "ack_refused", message)
        return {"halted": False, "message": message}

    return app
