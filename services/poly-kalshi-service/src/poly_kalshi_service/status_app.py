"""``pk-status``: the read-only status page, a separate process on 127.0.0.1:8051.

| Route | |
|---|---|
| ``GET /`` | the page |
| ``GET /api/summary`` | allowlisted live status, paper results and gate progress |
| ``GET /health/live`` | process up |

**Load on the trader is fixed, not per viewer.** One background poller reads the
service's ``/v1/status`` every ``LIVE_POLL_SECONDS`` and the journal on disk
every ``JOURNAL_EVERY``; requests are answered from that cache. Ten open tabs
cost the trading process exactly what none do. Results (daily P&L, equity
curve, trades, gates) come from the journal files, never from the service.

There is no write route. The service's status is read server-side with its API
key, which never leaves this process; every field returned is picked from an
allowlist below. Publish it only behind Cloudflare Access
(infra/cloudflare/README.md).

    pk-status --profile dev
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from ta_core import base_parser, load_or_exit
from ta_core.logging_config import configure_logging

from .config import Settings, load_settings
from .gates import evaluate
from .results import paper_results
from .status_page import STATUS_HTML
from .trades import read_journal

__all__ = ["LOGGER_NAME", "create_status_app", "pick", "run"]

LOGGER_NAME = "poly_kalshi_service.status"
LIVE_POLL_SECONDS = 5.0
JOURNAL_EVERY = 30.0
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


def create_status_app(
    settings: Settings, *, fetch: Fetch | None = None, poll: bool = True
) -> FastAPI:
    state: dict[str, Any] = {
        "live": None,
        "live_at": None,
        "journal": None,
        "journal_at": 0.0,
    }
    journal_dir = settings.state_dir / "journal"

    def read_results() -> dict[str, Any]:
        records = list(read_journal(journal_dir)) if journal_dir.is_dir() else []
        return {
            "gates": evaluate(records, settings.max_entry_px),
            "results": paper_results(records),
        }

    async def refresh_live(fetcher: Fetch) -> None:
        state["live"] = pick(await fetcher())
        state["live_at"] = time.time()

    async def refresh_journal() -> None:
        state["journal"] = await asyncio.to_thread(read_results)
        state["journal_at"] = time.monotonic()

    async def poller(fetcher: Fetch) -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_live(fetcher)
                if time.monotonic() - state["journal_at"] >= JOURNAL_EVERY:
                    await refresh_journal()
            await asyncio.sleep(LIVE_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as http:
            fetcher = fetch or _service_fetcher(settings, http)
            app.state.fetch = fetcher
            task = asyncio.create_task(poller(fetcher), name="pk-status-poller") if poll else None
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(
        title="pk-status", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    # The tunnel carries the page and its JSON; compress what is worth it.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def headers(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return STATUS_HTML

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        # Only the first request after a start may wait for data; never per request after.
        if state["live_at"] is None:
            await refresh_live(app.state.fetch)
        if state["journal"] is None:
            await refresh_journal()
        journal = state["journal"] or {}
        return {
            "fetched_at": state["live_at"],
            "service_up": state["live"] is not None,
            "live": state["live"],
            "gates": journal.get("gates"),
            "results": journal.get("results"),
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
