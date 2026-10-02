"""``ofi-status``: the read-only status page for colleagues.

A separate process from the scalper, on ``OFI_STATUS_HOST:OFI_STATUS_PORT``
(127.0.0.1:8040), meant to be published only through a Cloudflare Tunnel with
Cloudflare Access in front (infra/cloudflare/README.md).

| Route | |
|---|---|
| ``GET /`` | the page |
| ``GET /api/summary`` | roadmap, collection, health, research, trading |
| ``GET /api/history`` | the last 24 h of one-minute health points |
| ``GET /health/live`` | process up |

There is no write route of any kind. The scalper's status is read server-side
with its API key, which never leaves this process; every field returned is
picked from an allowlist in ``status.py``.

    ofi-status --profile dev
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from ta_core import base_parser, load_or_exit, serve
from ta_core.logging_config import configure_logging, log_event

from . import status as builders
from .config import ExecutionMode, Settings, load_settings
from .status_page import STATUS_HTML

__all__ = ["LOGGER_NAME", "create_status_app", "run"]

LOGGER_NAME = "ofi_scalper_service.status"
POLL_SECONDS = 30.0
HISTORY_EVERY = 60.0
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

Fetch = Callable[[], Awaitable[dict[str, Any] | None]]


def _scalper_fetcher(settings: Settings, http: httpx.AsyncClient) -> Fetch:
    url = f"http://127.0.0.1:{settings.port}/v1/status"
    headers = {"X-API-Key": settings.api_key.get_secret_value()}

    async def fetch() -> dict[str, Any] | None:
        try:
            response = await http.get(url, headers=headers, timeout=5.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        body = response.json()
        return body if isinstance(body, dict) else None

    return fetch


def create_status_app(
    settings: Settings,
    *,
    fetch: Fetch | None = None,
    units: Callable[[], dict[str, str]] = builders.unit_states,
    poll: bool = True,
) -> FastAPI:
    state: dict[str, Any] = {"status": None, "fetched_at": None, "last_ok_at": None}
    history = builders.History()
    http: httpx.AsyncClient | None = None

    collection = builders.Cached(
        lambda: builders.collection(
            record_dir=settings.record_dir,
            state_dir=settings.state_dir,
            host_tag=settings.host_tag,
            min_free_disk_gb=settings.min_free_disk_gb,
        )
    )
    research = builders.Cached(lambda: builders.research(settings.research_dir, settings.model_dir))
    trading = builders.Cached(
        lambda: builders.trading(
            settings.state_dir,
            model_loaded=settings.model_version is not None
            and settings.execution_mode is not ExecutionMode.OFF,
        )
    )
    unit_cache = builders.Cached(units, ttl=POLL_SECONDS)

    async def refresh() -> None:
        assert fetcher is not None
        status = await fetcher()
        now = time.time()
        state["status"], state["fetched_at"] = status, now
        if status is not None:
            state["last_ok_at"] = now

    async def poller() -> None:
        last_point = 0.0
        while True:
            with contextlib.suppress(Exception):
                await refresh()
                if time.time() - last_point >= HISTORY_EVERY:
                    history.add(state["status"], time.time())
                    last_point = time.time()
            await asyncio.sleep(POLL_SECONDS)

    fetcher: Fetch | None = fetch

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal http, fetcher
        if fetcher is None:
            http = httpx.AsyncClient()
            fetcher = _scalper_fetcher(settings, http)
        task = asyncio.create_task(poller(), name="ofi-status-poller") if poll else None
        log_event("ofi_status_starting", port=settings.status_port)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if http is not None:
                await http.aclose()

    app = FastAPI(
        title="ofi-status",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return STATUS_HTML

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        if state["fetched_at"] is None and fetcher is not None:
            await refresh()  # the first request after start: do not wait for the poller
        unit_states, data, results, trades = await asyncio.gather(
            asyncio.to_thread(unit_cache.get),
            asyncio.to_thread(collection.get),
            asyncio.to_thread(research.get),
            asyncio.to_thread(trading.get),
        )
        return {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "profile": settings.profile,
            "symbols": list(settings.binance_futures_symbols),
            "roadmap": builders.roadmap(),
            "collection": data,
            "health": builders.health(
                state["status"],
                unit_states,
                fetched_at=state["fetched_at"],
                last_ok_at=state["last_ok_at"],
            ),
            "research": results,
            "trading": trades,
        }

    @app.get("/api/history")
    async def history_route() -> dict[str, Any]:
        return {"points": history.series()}

    return app


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("Read-only status page for the OFI scalper (for colleagues)")
    args = parser.parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)
    configure_logging(settings.log_level, name=LOGGER_NAME)
    bind = settings.model_copy(update={"host": settings.status_host, "port": settings.status_port})
    logging.getLogger(LOGGER_NAME).info("ofi-status on %s:%s", bind.host, bind.port)

    def app_factory() -> object:
        return create_status_app(settings)

    serve(bind, app_factory, logger_name=LOGGER_NAME)


if __name__ == "__main__":
    run()
