"""The live process: feeds -> one clock -> recorder + engine; window and resolution loops.

``ingest(venue, text)`` is the only way a line enters. It stamps the line from
one strictly increasing clock, writes it to the tape, then hands it to the
engine, all on the event loop. That makes the tape a faithful, ordered record
of what the engine saw, and ``research.replay`` re-runs it exactly.

Loops:

- **windows**: every ``TICK_SECONDS``, make sure the current window and (inside
  ``discover_lead_seconds``) the next one are identified for every asset,
  writing each identity to the tape as a control line before subscribing to
  its ids. Alerts if a window is still unidentified 30 s after it opened.
- **resolutions**: each venue's outcome for every closed window, polled until
  known, written as control lines (the engine settles paper positions from
  Polymarket's). Polymarket's ``market_resolved`` WS event usually beats it.
- **health**: stale feeds, heartbeat log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
from ta_core.logging_config import log_event

from .alerts import Alerts
from .config import Settings
from .decision import WINDOW_S
from .engine import Engine
from .feeds import KalshiFeed, PolyFeed
from .recorder import Recorder
from .windows import Discovery, Window, window_open

__all__ = ["Runtime"]

NS = 1_000_000_000
TICK_SECONDS = 2.0
RESOLUTION_POLL_SECONDS = 15.0
RESOLUTION_GIVE_UP_S = 3600
RECYCLE_POLY_AFTER_REMOVED = 60  # ~6 windows of 5 assets x 2 tokens


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        engine: Engine,
        discovery: Discovery,
        kalshi: KalshiFeed | None,
        poly: PolyFeed | None,
        alerts: Alerts,
        recorder: Recorder | None = None,
        http: httpx.AsyncClient | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.discovery = discovery
        self.kalshi = kalshi
        self.poly = poly
        self.alerts = alerts
        self.recorder = recorder
        self.http = http
        self._clock = clock_ns
        self._last_ns = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._discover_failed: dict[tuple[str, int], float] = {}
        self._resolution_given_up: set[tuple[str, int, str]] = set()
        self.started_ns: int | None = None
        self.lines = 0

    # --- the one entry point ----------------------------------------------------

    def ingest(self, venue: str, text: str) -> None:
        if "\n" in text or "\r" in text:
            # The tape is one frame per line. A raw newline in valid JSON can only
            # be whitespace (inside strings it is escaped), so this loses nothing;
            # some venue frames arrive with a trailing one.
            text = text.replace("\r", " ").replace("\n", " ").strip()
        recv_ns = max(self._last_ns + 1, self._clock())
        self._last_ns = recv_ns
        self.lines += 1
        if self.recorder is not None:
            self.recorder.write(venue, recv_ns, text)
        started = time.perf_counter_ns()
        try:
            self.engine.handle_line(recv_ns, venue, text)
            self.engine.metrics.add("engine_us", (time.perf_counter_ns() - started) / 1000)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the feed
            self.engine.counters["engine_errors"] += 1
            log_event(
                "engine_error",
                level=logging.ERROR,
                venue=venue,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )

    def control(self, body: dict[str, Any]) -> None:
        self.ingest("c", json.dumps(body, separators=(",", ":")))

    # --- lifecycle --------------------------------------------------------------

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log_event(
                "task_died",
                level=logging.ERROR,
                task=task.get_name(),
                error=repr(task.exception())[:200],
            )
            self.alerts.send(
                f"task:{task.get_name()}",
                "poly-kalshi: a background task died",
                [task.get_name(), repr(task.exception())[:300]],
            )

    async def start(self) -> None:
        self.started_ns = self._clock()
        if self.recorder is not None:
            self.recorder.start()
        if self.kalshi is not None:
            self.spawn(self.kalshi.run(), "pk-kalshi")
        if self.poly is not None:
            self.spawn(self.poly.run(), "pk-poly")
        self.spawn(self._loop(self.window_tick, TICK_SECONDS), "pk-windows")
        self.spawn(self._loop(self.resolution_tick, RESOLUTION_POLL_SECONDS), "pk-resolutions")
        self.spawn(self._loop_lag(), "pk-loop-lag")
        self.spawn(
            self._loop(self.health_tick, max(self.settings.heartbeat_seconds, 5)), "pk-health"
        )

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        if self.recorder is not None:
            self.recorder.stop()
        await self.alerts.drain()

    async def _loop(self, tick: Callable[[], Coroutine[Any, Any, None]], seconds: float) -> None:
        while True:
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive one bad tick
                log_event(
                    "tick_failed",
                    level=logging.WARNING,
                    tick=getattr(tick, "__name__", "?"),
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
            await asyncio.sleep(seconds)

    async def _loop_lag(self, every: float = 0.5) -> None:
        """How late the event loop wakes: the feeds' backlog shows here first."""
        while True:
            started = time.monotonic()
            await asyncio.sleep(every)
            self.engine.metrics.add("loop_lag_ms", (time.monotonic() - started - every) * 1000)

    # --- windows ----------------------------------------------------------------

    def _targets(self, now: float) -> list[int]:
        current = window_open(now)
        targets = [current]
        if current + WINDOW_S - now <= self.settings.discover_lead_seconds:
            targets.append(current + WINDOW_S)
        return targets

    async def _discover(self, asset: str, open_unix: int, now: float) -> None:
        if open_unix in self.engine.windows.get(asset, {}):
            return
        failed_at = self._discover_failed.get((asset, open_unix))
        if failed_at is not None and now - failed_at < 5:
            return
        try:
            window = await self.discovery.discover(asset, open_unix)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            window = None
            log_event(
                "discover_failed",
                level=logging.WARNING,
                asset=asset,
                open_unix=open_unix,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        if window is None:
            self._discover_failed[(asset, open_unix)] = now
            return
        self._discover_failed.pop((asset, open_unix), None)
        self.control(window.control())
        log_event(
            "window_identified", asset=asset, open_unix=open_unix, kalshi=window.kalshi_ticker
        )

    async def window_tick(self) -> None:
        now = self._clock() / NS
        targets = self._targets(now)
        await asyncio.gather(
            *(self._discover(a, o, now) for a in self.settings.assets for o in targets)
        )
        live = [
            w
            for a in self.settings.assets
            for o in targets
            if (w := self.engine.windows[a].get(o)) is not None
        ]
        if self.kalshi is not None:
            await self.kalshi.set_wanted({w.kalshi_ticker for w in live})
        if self.poly is not None:
            await self.poly.set_wanted({t for w in live for t in (w.yes_token, w.no_token)})
            sec_into = now - targets[0]
            if self.poly.removed_since_connect >= RECYCLE_POLY_AFTER_REMOVED and 1 <= sec_into < 8:
                # Inside entry_early_s: no decision can need these books yet.
                log_event("poly_recycle", removed=self.poly.removed_since_connect)
                await self.poly.recycle()
        sec_into = now - targets[0]
        if sec_into > 30:
            missing = [a for a in self.settings.assets if targets[0] not in self.engine.windows[a]]
            if missing:
                self.alerts.send(
                    "unidentified",
                    "poly-kalshi: window not identified",
                    [f"open_unix={targets[0]} assets={','.join(missing)}"],
                )

    # --- resolutions ------------------------------------------------------------

    def _unresolved(self, now: float) -> list[tuple[Window, str]]:
        work = []
        for asset, windows in self.engine.windows.items():
            for open_unix, window in windows.items():
                age = now - window.close_unix
                if age < 20:
                    continue
                known = self.engine.resolutions.get((asset, open_unix), {})
                for venue in ("poly", "kalshi"):
                    key = (asset, open_unix, venue)
                    if venue in known or key in self._resolution_given_up:
                        continue
                    if age > RESOLUTION_GIVE_UP_S:
                        self._resolution_given_up.add(key)
                        log_event("resolution_given_up", level=logging.WARNING, key=list(key))
                        continue
                    work.append((window, venue))
        return work

    async def _poll(self, window: Window, venue: str) -> None:
        try:
            if venue == "poly":
                outcome = await self.discovery.poly_outcome(window)
            else:
                outcome = await self.discovery.kalshi_outcome(window)
        except Exception as exc:  # noqa: BLE001 - polled again next tick
            log_event(
                "resolution_poll_failed",
                level=logging.WARNING,
                venue=venue,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            return
        if outcome is not None:
            self.control(
                {
                    "type": "resolution",
                    "venue": venue,
                    "asset": window.asset,
                    "open_unix": window.open_unix,
                    "outcome": outcome,
                }
            )

    async def _rediscover_open_positions(self) -> None:
        """After a restart, open positions need their window back to be settled."""
        for position in self.engine.ledger.open_positions():
            if position.open_unix in self.engine.windows.get(position.asset, {}):
                continue
            try:
                window = await self.discovery.discover(position.asset, position.open_unix)
            except Exception:  # noqa: BLE001 - tried again next tick
                continue
            if window is not None:
                self.control(window.control())

    async def resolution_tick(self) -> None:
        await self._rediscover_open_positions()
        work = self._unresolved(self._clock() / NS)
        for start in range(0, len(work), 8):
            await asyncio.gather(*(self._poll(w, v) for w, v in work[start : start + 8]))

    # --- health -----------------------------------------------------------------

    async def health_tick(self) -> None:
        now_ns = self._clock()
        stale = self.settings.stale_feed_seconds
        for venue, feed in (("k", self.kalshi), ("p", self.poly)):
            if feed is None:
                continue
            last = self.engine.last_by_venue.get(venue)
            started = self.started_ns or now_ns
            age = (now_ns - (last or started)) / NS
            if age > stale:
                self.alerts.send(
                    f"stale:{venue}",
                    f"poly-kalshi: {'Kalshi' if venue == 'k' else 'Polymarket'} feed stale",
                    [f"no message for {age:.0f}s", f"last_error={feed.last_error}"],
                )
        if self.settings.heartbeat_seconds:
            ledger = self.engine.ledger.snapshot(now_ns)
            log_event(
                "heartbeat",
                lines=self.lines,
                books=len(self.engine.books),
                counters=dict(self.engine.counters),
                cash=ledger["cash"],
                open_positions=ledger["open_positions"],
            )

    # --- control and status -----------------------------------------------------

    def kill(self, source: str, reason: str = "") -> bool:
        fired = self.engine.risk.kill(source, reason)
        if fired:
            self.alerts.send(
                "kill", "poly-kalshi HALTED", [f"source={source}", reason or "-"], always=True
            )
        return fired

    def ready(self) -> tuple[bool, dict[str, Any]]:
        feeds = {
            "kalshi": self.kalshi.connected if self.kalshi else None,
            "poly": self.poly.connected if self.poly else None,
        }
        identified = {a: s.window is not None for a, s in self.engine.state.items()}
        ok = all(v is not False for v in feeds.values()) and all(identified.values())
        return ok, {"feeds": feeds, "identified": identified}

    def status(self) -> dict[str, Any]:
        return {
            "profile": self.settings.profile,
            "execution_mode": self.settings.execution_mode.value,
            "host": self.settings.host_tag,
            "assets": list(self.settings.assets),
            "params": {
                "gap_threshold": self.settings.gap_threshold,
                "max_entry_px": self.settings.max_entry_px,
                "notional_usd": self.settings.notional_usd,
                "entry_early_s": self.settings.entry_early_s,
                "entry_late_s": self.settings.entry_late_s,
                "paper_latency_ms": self.settings.paper_latency_ms,
            },
            "started_ns": self.started_ns,
            "lines": self.lines,
            "feeds": {
                "kalshi": self.kalshi.stats() if self.kalshi else None,
                "poly": self.poly.stats() if self.poly else None,
            },
            "recorder": self.recorder.stats() if self.recorder else {"enabled": False},
            "alerts": {"recent": self.alerts.sent[-10:], "suppressed": self.alerts.suppressed},
            "engine": self.engine.status(),
            "recent": list(self.engine.recent)[-50:][::-1],
        }
