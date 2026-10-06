"""``pk-status``: the read-only status page, a separate process on 127.0.0.1:8051.

| Route | |
|---|---|
| ``GET /`` | the page |
| ``GET /api/summary`` | allowlisted live status, latency, paper results and gate progress |
| ``GET /api/state`` | market state only (quotes, delta, rule preview, book ages) |
| ``GET /api/series?since=`` | per-second market data per asset for the charts, and decisions |
| ``GET /api/export`` | one zip with everything for offline analysis (``export.py``) |
| ``GET /health/live`` | process up |

**Load on the trader is fixed, not per viewer.** One background poller reads the
service's ``/v1/status`` every ``LIVE_POLL_SECONDS`` and the journal on disk
every ``JOURNAL_EVERY``, and pulls only the *new* per-second price points
from ``/v1/series``; requests are answered from that cache. Ten open tabs
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
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from ta_core import base_parser, load_or_exit
from ta_core.logging_config import configure_logging

from .config import Settings, load_settings
from .export import build_export, export_name
from .gates import evaluate
from .results import paper_results
from .status_page import STATUS_HTML
from .trades import in_epoch, read_journal

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

SERIES_POINTS = 1800  # two windows per asset
Fetch = Callable[[], Awaitable[dict[str, Any] | None]]
FetchSeries = Callable[[int], Awaitable[dict[str, Any] | None]]


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
        "epoch": status.get("epoch"),
        "build": status.get("build"),
        "config": status.get("config"),
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
                    "gap",
                    "preview",
                    "decision",
                    "age_ms",
                    "legs",
                    "kalshi",
                    "poly_yes",
                    "poly_no",
                ),
            )  # fmt: skip
            for name, a in (engine.get("assets") or {}).items()
        },
        "feeds": {
            name: _keep(
                f, ("connected", "connects", "messages", "errors", "seq_gaps", "slow_consumer")
            )
            for name, f in feeds.items()
        },
        "feed_age_s": engine.get("feed_age_s"),
        "recorder": _keep(
            recorder,
            ("enabled", "lines_written", "dropped", "errors", "disk_free_gb", "disk_paused"),
        ),
        "latency": engine.get("latency"),
        "host_load": status.get("host_load"),
        "telegram": _keep(status.get("telegram"), ("handled", "errors", "conflicts")),
        "ledger": engine.get("ledger"),
        "risk": _keep(engine.get("risk"), ("halted", "kill_file_present")),
        "counters": {
            k: v
            for k, v in (engine.get("counters") or {}).items()
            if k.startswith(
                (
                    "fills",
                    "no_fills",
                    "settled",
                    "blocked_",
                    "bbo_",
                    "resync",
                    "reconnect",
                    "invalid_",
                    "windows_unidentified",
                    "engine_errors",
                )  # fmt: skip
            )
        },
        "recent": [_keep(r, RECENT_FIELDS) for r in (status.get("recent") or [])[:30]],
    }


def _service_fetchers(settings: Settings, http: httpx.AsyncClient) -> tuple[Fetch, FetchSeries]:
    base = f"http://127.0.0.1:{settings.port}"
    headers = {"X-API-Key": settings.api_key.get_secret_value()}

    async def get(path: str, **params: Any) -> dict[str, Any] | None:
        try:
            response = await http.get(base + path, params=params, headers=headers, timeout=5.0)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    async def fetch() -> dict[str, Any] | None:
        return await get("/v1/status")

    async def fetch_series(since: int) -> dict[str, Any] | None:
        return await get("/v1/series", since=since)

    return fetch, fetch_series


def create_status_app(
    settings: Settings,
    *,
    fetch: Fetch | None = None,
    fetch_series: FetchSeries | None = None,
    poll: bool = True,
) -> FastAPI:
    state: dict[str, Any] = {
        "live": None,
        "live_at": None,
        "journal": None,
        "journal_at": 0.0,
        "rates": {},
        "last_messages": {},
        "series_last": 0,
    }
    series: dict[str, deque[list[Any]]] = {a: deque(maxlen=SERIES_POINTS) for a in settings.assets}
    journal_dir = settings.state_dir / "journal"
    export_lock = asyncio.Lock()

    def read_results() -> dict[str, Any]:
        records = list(read_journal(journal_dir)) if journal_dir.is_dir() else []
        # Only the current results epoch: earlier epochs were invalidated by a fix.
        kept, excluded = in_epoch(records, settings.results_epoch)
        return {
            "epoch": settings.results_epoch,
            "excluded_records": excluded,
            "gates": evaluate(kept, settings.max_entry_px),
            "results": paper_results(kept),
        }

    def note_rates(live: dict[str, Any] | None, now: float) -> None:
        for name, feed in ((live or {}).get("feeds") or {}).items():
            if not feed or feed.get("messages") is None:
                continue
            prev = state["last_messages"].get(name)
            if prev is not None and now > prev[1] and feed["messages"] >= prev[0]:
                state["rates"][name] = round((feed["messages"] - prev[0]) / (now - prev[1]), 1)
            state["last_messages"][name] = (feed["messages"], now)

    async def refresh_live(fetcher: Fetch) -> None:
        state["live"] = pick(await fetcher())
        state["live_at"] = time.time()
        note_rates(state["live"], state["live_at"])

    async def refresh_series(fetcher: FetchSeries) -> None:
        body = await fetcher(state["series_last"])
        for asset, points in ((body or {}).get("points") or {}).items():
            if asset not in series:
                continue
            for point in points:
                series[asset].append(list(point))
                state["series_last"] = max(state["series_last"], int(point[0]))

    async def refresh_journal() -> None:
        state["journal"] = await asyncio.to_thread(read_results)
        state["journal_at"] = time.monotonic()

    async def poller(fetcher: Fetch, series_fetcher: FetchSeries | None) -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_live(fetcher)
                if series_fetcher is not None:
                    await refresh_series(series_fetcher)
                if time.monotonic() - state["journal_at"] >= JOURNAL_EVERY:
                    await refresh_journal()
            await asyncio.sleep(LIVE_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as http:
            default_fetch, default_series = _service_fetchers(settings, http)
            fetcher = fetch or default_fetch
            series_fetcher = fetch_series if fetch is not None else default_series
            app.state.fetch = fetcher
            app.state.fetch_series = series_fetcher
            task = (
                asyncio.create_task(poller(fetcher, series_fetcher), name="pk-status-poller")
                if poll
                else None
            )
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

    async def ensure_fresh() -> None:
        # Only the first request after a start may wait for data; never per request after.
        if state["live_at"] is None:
            await refresh_live(app.state.fetch)
            if app.state.fetch_series is not None:
                await refresh_series(app.state.fetch_series)
        if state["journal"] is None:
            await refresh_journal()

    def summary_body() -> dict[str, Any]:
        journal = state["journal"] or {}
        return {
            "fetched_at": state["live_at"],
            "service_up": state["live"] is not None,
            "live": state["live"],
            "rates": state["rates"],
            "epoch": journal.get("epoch"),
            "excluded_records": journal.get("excluded_records"),
            "gates": journal.get("gates"),
            "results": journal.get("results"),
        }

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return STATUS_HTML

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        await ensure_fresh()
        return summary_body()

    @app.get("/api/state")
    async def market_state() -> dict[str, Any]:
        await ensure_fresh()
        live = state["live"] or {}
        return {
            "fetched_at": state["live_at"],
            "service_up": state["live"] is not None,
            "params": live.get("params"),
            "feed_age_s": live.get("feed_age_s"),
            "rates": state["rates"],
            "assets": live.get("assets") or {},
        }

    @app.get("/api/series")
    async def series_route(since: int = Query(default=0, ge=0)) -> dict[str, Any]:
        await ensure_fresh()
        live = state["live"] or {}
        decisions = [r for r in live.get("recent") or [] if r and r.get("type") == "decision"]
        return {
            "threshold": (live.get("params") or {}).get("gap_threshold"),
            "entry_early_s": (live.get("params") or {}).get("entry_early_s"),
            "entry_late_s": (live.get("params") or {}).get("entry_late_s"),
            "points": {a: [p for p in pts if p[0] > since] for a, pts in series.items()},
            "decisions": decisions,
        }

    @app.get("/api/export")
    async def export() -> FileResponse:
        await ensure_fresh()
        async with export_lock:  # one export at a time; it reads every journal file
            path: Path = await asyncio.to_thread(
                build_export,
                settings,
                summary=summary_body(),
                series={a: list(pts) for a, pts in series.items()},
            )
        return FileResponse(
            path,
            media_type="application/zip",
            filename=export_name(settings),
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

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
