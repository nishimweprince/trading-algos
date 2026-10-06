"""``pk-status``: the read-only status page, a separate process on 127.0.0.1:8051.

| Route | |
|---|---|
| ``GET /`` | the page |
| ``GET /api/summary`` | allowlisted live status plus gate progress from the journal |
| ``GET /health/live`` | process up |

There is no write route. The service's status is read server-side with its API
key, which never leaves this process; every field returned is picked from an
allowlist below. Publish it, if at all, only behind an authenticating proxy
(see the ofi-scalper Cloudflare Tunnel + Access recipe).

    pk-status --profile dev
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from ta_core import base_parser, load_or_exit
from ta_core.logging_config import configure_logging

from .config import Settings, load_settings
from .gates import evaluate
from .status_page import STATUS_HTML
from .trades import read_journal

__all__ = ["LOGGER_NAME", "create_status_app", "pick", "run"]

LOGGER_NAME = "poly_kalshi_service.status"
GATES_EVERY = 60.0
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
RECENT_FIELDS = (
    "type", "asset", "open_unix", "t_ns", "sec_into", "action", "reason", "gap", "side",
    "entry_px", "contracts", "blocked", "shares", "vwap", "fee", "touch_px", "outcome", "pnl",
)  # fmt: skip

Fetch = Callable[[], Awaitable[dict[str, Any] | None]]


def _keep(body: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any] | None:
    return None if body is None else {k: body.get(k) for k in keys}


def pick(status: dict[str, Any] | None) -> dict[str, Any] | None:
    """The allowlisted subset of ``/v1/status``: nothing else leaves the process."""
    if status is None:
        return None
    engine = status.get("engine") or {}
    feeds = status.get("feeds") or {}
    recorder = status.get("recorder") or {}
    return {
        "mode": status.get("execution_mode"),
        "host": status.get("host"),
        "params": status.get("params"),
        "assets": {
            name: _keep(
                a,
                (
                    "open_unix",
                    "sec_into",
                    "identified",
                    "traded",
                    "action",
                    "last_gap",
                    "max_abs_gap",
                    "kalshi",
                    "poly_yes",
                    "poly_no",
                ),
            )  # fmt: skip
            for name, a in (engine.get("assets") or {}).items()
        },
        "feeds": {
            name: _keep(f, ("connected", "connects", "messages", "errors", "seq_gaps"))
            for name, f in feeds.items()
        },
        "feed_age_s": engine.get("feed_age_s"),
        "recorder": _keep(
            recorder,
            ("enabled", "lines_written", "dropped", "errors", "disk_free_gb", "disk_paused"),
        ),
        "ledger": engine.get("ledger"),
        "risk": _keep(engine.get("risk"), ("halted", "kill_file_present")),
        "counters": {
            k: v
            for k, v in (engine.get("counters") or {}).items()
            if k.startswith(
                ("fills", "no_fills", "settled", "blocked_", "bbo_", "resync", "reconnect")
            )
        },
        "recent": [_keep(r, RECENT_FIELDS) for r in (status.get("recent") or [])[:30]],
    }


def _service_fetcher(settings: Settings, http: httpx.AsyncClient) -> Fetch:
    url = f"http://127.0.0.1:{settings.port}/v1/status"
    headers = {"X-API-Key": settings.api_key.get_secret_value()}

    async def fetch() -> dict[str, Any] | None:
        try:
            response = await http.get(url, headers=headers, timeout=5.0)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    return fetch


def create_status_app(settings: Settings, *, fetch: Fetch | None = None) -> FastAPI:
    cache: dict[str, Any] = {"gates": None, "gates_at": 0.0}
    journal = settings.state_dir / "journal"

    def gates() -> dict[str, Any] | None:
        if time.monotonic() - cache["gates_at"] > GATES_EVERY or cache["gates"] is None:
            cache["gates"] = evaluate(read_journal(journal), settings.max_entry_px)
            cache["gates_at"] = time.monotonic()
        return cache["gates"]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as http:
            app.state.fetch = fetch or _service_fetcher(settings, http)
            yield

    app = FastAPI(title="pk-status", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def headers(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return STATUS_HTML

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        live = pick(await app.state.fetch())
        return {
            "fetched_at": time.time(),
            "service_up": live is not None,
            "live": live,
            "gates": await asyncio.to_thread(gates),
        }

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("pk-status: read-only poly-kalshi status page")
    args = parser.parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)
    configure_logging(settings.log_level, name=LOGGER_NAME)
    import uvicorn

    uvicorn.run(create_status_app(settings), host=settings.status_host, port=settings.status_port)
