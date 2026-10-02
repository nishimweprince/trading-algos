"""The live loop: stream -> book/state engine -> gate -> risk, plus the watchers.

One consumer reads :meth:`FuturesStreams.events` in arrival order. Before
feeding each event it samples every grid time the event has passed
(``GridClock``), so a live sample equals a replay of the recording. A timer
covers quiet markets the same way.

Stage 0–1 has no model, no policy and no order path. Where a later stage will
cancel and flatten, this logs what it would do and alerts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from typing import Any

from ta_core.logging_config import log_event
from ta_plugin_binance_futures.depth import SyncOutcome, SyncState
from ta_plugin_binance_futures.streams import (
    AggTrade,
    BookSnapshot,
    BookStatus,
    BookTick,
    DepthUpdate,
    Liquidation,
    MarkPrice,
    StreamStatus,
)

from .alerts import Alerts
from .recorder import Recorder
from .regime_gate import RegimeGate
from .risk import RiskState
from .state_engine import EngineConfig, GridClock, MarketState

__all__ = ["ScalperRuntime"]

GATE_SECONDS = 5.0
KILL_POLL_SECONDS = 1.0
HEALTH_SECONDS = 10.0
LATENCY_RESERVOIR = 2000
CROSS_PAIRS = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"}


def _percentiles(values: deque[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)

    return {"p50": pick(0.50), "p90": pick(0.90), "p99": pick(0.99), "n": len(ordered)}


class ScalperRuntime:
    def __init__(
        self,
        settings: Any,
        *,
        streams: Any,
        account: Any,
        risk: RiskState,
        gate: RegimeGate,
        alerts: Alerts,
        recorder: Recorder | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.settings = settings
        self.streams = streams
        self.account = account
        self.risk = risk
        self.gate = gate
        self.alerts = alerts
        self.recorder = recorder
        self._clock_ns = clock_ns
        self.grid_ns = settings.grid_ms * 1_000_000
        self.grid = GridClock(self.grid_ns)
        self.engine: MarketState | None = None
        self.latest: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.fees: dict[str, Any] = {}
        self.counts: Counter[str] = Counter()
        self.feed_latency_ms: dict[str, deque[float]] = {}
        self.last_book_ns: dict[str, int] = {}
        self.booted = asyncio.Event()
        self.boot_error: str | None = None
        self._burst: Counter[str] = Counter()
        self.key_check: dict[str, Any] = {"status": "pending"}
        self.last_heartbeat: dict[str, Any] | None = None
        self._heartbeat_counts: Counter[str] = Counter()
        self._heartbeat_lines = 0
        self._heartbeat_at = time.monotonic()
        self._started_at = time.monotonic()
        self._disk_alerted = False
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if self.recorder is not None:
            self.recorder.start()
        self.spawn(self._boot(), "ofi-boot")
        self.spawn(self._kill_file_watcher(), "ofi-kill-file")
        self.spawn(self._health_loop(), "ofi-health")

    def spawn(self, coroutine: Any, name: str) -> None:
        self._tasks.append(asyncio.create_task(coroutine, name=name))

    async def _boot(self) -> None:
        """Load contract specs (retrying), then start consuming."""
        delay = 1.0
        while True:
            try:
                await self.streams.start()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep trying, stay not-ready
                self.boot_error = f"{type(exc).__name__}: {exc}"[:200]
                log_event("ofi_boot_failed", level=logging.ERROR, error=self.boot_error)
                self.alerts.send("boot", "OFI scalper cannot start streams", [self.boot_error])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        self.boot_error = None
        ticks = {
            symbol: info.price_increment or 0.01
            for symbol, info in self.streams.instruments.items()
        }
        self.engine = MarketState(
            EngineConfig(
                tick_sizes=ticks,
                cross_pairs={s: p for s, p in CROSS_PAIRS.items() if s in ticks and p in ticks},
            )
        )
        self.booted.set()
        log_event("ofi_streams_started", symbols=list(ticks), tick_sizes=ticks)
        self.spawn(self._consume(), "ofi-consume")
        self.spawn(self._quiet_grid(), "ofi-quiet-grid")
        self.spawn(self._gate_loop(), "ofi-gate")
        self.spawn(self._load_fees(), "ofi-fees")
        self.spawn(self._check_key(), "ofi-key-check")

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self.streams.close()
        closer = getattr(self.account, "aclose", None)
        if closer is not None:
            await closer()
        if self.recorder is not None:
            await asyncio.to_thread(self.recorder.stop)
        await self.alerts.drain()

    # --- the consumer -----------------------------------------------------------

    async def _consume(self) -> None:
        async for event in self.streams.events():
            try:
                self.handle(event)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the feed
                self.counts["handler_errors"] += 1
                log_event("ofi_event_failed", level=logging.ERROR, error=repr(exc)[:200])

    def handle(self, event: Any) -> None:
        """Feed one event. Public so replays and tests drive the exact live path."""
        assert self.engine is not None
        recv_ns = getattr(event, "recv_ns", None)
        if recv_ns is not None:
            for grid_ns in self.grid.due(recv_ns):
                self._on_grid(grid_ns)
        self.counts[type(event).__name__] += 1

        if isinstance(event, DepthUpdate | BookSnapshot):
            self._latency("depth", event.recv_ns, event.event_ms)
            if event.outcome is SyncOutcome.APPLIED and event.symbol in self.engine.symbols:
                self.engine[event.symbol].on_book(
                    event.recv_ns, self.streams.syncs[event.symbol].book
                )
                self.last_book_ns[event.symbol] = event.recv_ns
        elif isinstance(event, AggTrade):
            self._latency("aggTrade", event.recv_ns, event.event_ms)
            if event.symbol in self.engine.symbols:
                self.engine[event.symbol].on_trade(
                    event.recv_ns, event.price, event.qty, event.buyer_is_maker
                )
                self._burst[event.symbol] += 1
                if self._burst[event.symbol] == self.settings.burst_trades:
                    self.latest[event.symbol] = {
                        **self.engine.sample(event.symbol, event.recv_ns + 1),
                        "trigger": "burst",
                    }
                    self.counts["burst_samples"] += 1
        elif isinstance(event, BookTick):
            self._latency("bookTicker", event.recv_ns, event.event_ms)
        elif isinstance(event, MarkPrice):
            if event.symbol in self.engine.symbols:
                self.engine[event.symbol].on_funding(
                    event.recv_ns, event.funding_rate, event.next_funding_ms
                )
        elif isinstance(event, Liquidation):
            self.gate.on_liquidation(event.symbol, event.recv_ns, event.avg_price * event.qty)
        elif isinstance(event, BookStatus):
            self._on_book_status(event)
        elif isinstance(event, StreamStatus):
            self._on_stream_status(event)

    def _on_grid(self, grid_ns: int) -> None:
        assert self.engine is not None
        self._burst.clear()
        for symbol, features in self.engine.grid(grid_ns).items():
            features["trigger"] = "grid"
            self.latest[symbol] = features
            self.gate.observe(features)
            last = self.last_book_ns.get(symbol)
            stale = last is None or (grid_ns - last) / 1e6 > self.settings.stale_book_ms
            if self.risk.set_pause(symbol, "stale_book", stale) and stale and last is not None:
                self._would("cancel_all", symbol, "stale_book")
        self.counts["grid_samples"] += 1

    async def _quiet_grid(self) -> None:
        """Sample on time even when no event arrives to drive the grid."""
        while True:
            await asyncio.sleep(self.grid_ns / 1e9)
            # Only when the consumer is idle on an empty queue: then every event
            # stamped before now has been fed, and the sample matches a replay.
            if self.engine is not None and self.streams.backlog == 0:
                for grid_ns in self.grid.due(self._clock_ns()):
                    self._on_grid(grid_ns)

    def _on_book_status(self, event: BookStatus) -> None:
        assert self.engine is not None
        live = event.state is SyncState.LIVE
        if event.symbol in self.engine.symbols:
            if live:
                # Diffs replayed from the buffer inside apply_snapshot never came
                # through as DepthUpdates; start the engine from the verified book.
                self.engine[event.symbol].on_book(
                    event.recv_ns, self.streams.syncs[event.symbol].book
                )
                self.last_book_ns[event.symbol] = event.recv_ns
            else:
                self.engine[event.symbol].on_book_reset(event.recv_ns)
                self.last_book_ns.pop(event.symbol, None)
        changed = self.risk.set_pause(event.symbol, "depth_resync", not live)
        if "gap" in event.reason or event.reason == "crossed":
            if self.recorder is not None:
                self.recorder.note_gap(event.symbol)
            self._would("cancel_all", event.symbol, f"depth_{event.reason}")
            self.alerts.send(
                f"gap:{event.symbol}",
                f"OFI {event.symbol}: depth {event.reason}, resyncing",
                [f"gaps={event.gaps} resyncs={event.resyncs}"],
            )
        if changed:
            log_event(
                "ofi_book_state", symbol=event.symbol, state=event.state.value, reason=event.reason
            )

    def _on_stream_status(self, event: StreamStatus) -> None:
        down = event.state != "connected"
        reason = f"stream_{event.channel}"
        for symbol in self.streams.symbols:
            self.risk.set_pause(symbol, reason, down)
        if down:
            self._would("cancel_all", "*", f"{event.channel}_{event.state}")
            self.alerts.send(
                f"stream:{event.channel}",
                f"OFI stream {event.channel} {event.state}",
                [event.error or "", "Reconnecting; books resync before resuming."],
            )

    def _latency(self, kind: str, recv_ns: int, event_ms: int) -> None:
        # Includes local clock offset vs Binance; ofi-latency measures and removes it.
        series = self.feed_latency_ms.setdefault(kind, deque(maxlen=LATENCY_RESERVOIR))
        series.append(recv_ns / 1e6 - event_ms)

    # --- slow loops -------------------------------------------------------------

    async def _gate_loop(self) -> None:
        while True:
            await asyncio.sleep(GATE_SECONDS)
            for symbol, features in list(self.latest.items()):
                self.decisions[symbol] = self.gate.decide(features).as_dict()

    async def _kill_file_watcher(self) -> None:
        path = self.settings.kill_file_path
        while True:
            await asyncio.sleep(KILL_POLL_SECONDS)
            if path.exists() and not self.risk.halted:
                self.kill("file", f"{path} exists")

    async def _load_fees(self) -> None:
        if self.settings.maker_fee_bp is not None and self.settings.taker_fee_bp is not None:
            self.fees = {
                "source": "settings",
                "maker_bp": self.settings.maker_fee_bp,
                "taker_bp": self.settings.taker_fee_bp,
                "bnb_discount": self.settings.bnb_fee_discount,
            }
            return
        if not self.account.available:
            self.fees = {"source": "unavailable", "detail": "no read-only key and no OFI_*_FEE_BP"}
            return
        try:
            rates = {s: await self.account.commission_rate(s) for s in self.streams.symbols}
            config = await self.account.account_config()
        except Exception as exc:  # noqa: BLE001
            self.fees = {"source": "error", "detail": str(exc)[:200]}
            return
        self.fees = {
            "source": "binance",
            "fee_tier": config.get("feeTier"),
            "bnb_discount": self.settings.bnb_fee_discount,
            "symbols": {
                s: {"maker_bp": r.maker_bp, "taker_bp": r.taker_bp} for s, r in rates.items()
            },
            "account": config,
        }
        log_event("ofi_fees_loaded", fees=self.fees)

    async def _check_key(self) -> None:
        """What the API key itself may do. The account's canTrade/canWithdraw
        (in fees.account) describe the account, not this key."""
        reader = getattr(self.account, "api_restrictions", None)
        if reader is None or not self.account.available:
            self.key_check = {"status": "skipped", "detail": "no key configured"}
            return
        try:
            restrictions = await reader()
        except Exception as exc:  # noqa: BLE001 - a failed check must not stop the feed
            self.key_check = {"status": "error", "detail": str(exc)[:200]}
            log_event("ofi_key_check_failed", level=logging.WARNING, error=self.key_check["detail"])
            return
        if restrictions is None:
            self.key_check = {"status": "skipped", "detail": "BINANCE_SAPI_URL is empty"}
            return
        self.key_check = {
            "status": "ok" if restrictions.read_only else "dangerous",
            "read_only": restrictions.read_only,
            "ip_restricted": restrictions.ip_restricted,
            "dangerous": list(restrictions.dangerous),
            "permissions": restrictions.raw,
        }
        log_event(
            "ofi_key_check",
            level=logging.INFO if restrictions.read_only else logging.ERROR,
            read_only=restrictions.read_only,
            ip_restricted=restrictions.ip_restricted,
            dangerous=list(restrictions.dangerous),
        )
        if restrictions.dangerous:
            self.alerts.send(
                "key",
                "OFI: the read-only Binance key has extra permissions",
                [
                    "Enabled: " + ", ".join(restrictions.dangerous),
                    "This service only reads. Disable these on the key in API Management.",
                ],
                always=True,
            )
        elif not restrictions.ip_restricted:
            self.alerts.send(
                "key-ip",
                "OFI: the Binance key is not IP-restricted",
                ["Restrict it to this host's static IP in API Management."],
            )

    # --- health -------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Disk alerts every few seconds; a heartbeat line every OFI_HEARTBEAT_SECONDS.

        On a headless host the heartbeat is how silence becomes informative:
        no heartbeat means a stalled process, not a quiet market.
        """
        every = self.settings.heartbeat_seconds
        while True:
            await asyncio.sleep(HEALTH_SECONDS if not every else min(HEALTH_SECONDS, every))
            self._check_disk_alert()
            if every and time.monotonic() - self._heartbeat_at >= every:
                self.heartbeat()

    def _check_disk_alert(self) -> None:
        if self.recorder is None:
            return
        if self.recorder.disk_paused and not self._disk_alerted:
            self._disk_alerted = True
            stats = self.recorder.stats()
            self.alerts.send(
                "disk",
                "OFI recorder stopped: disk nearly full",
                [
                    f"free {stats['disk_free_gb']} GB < floor {stats['min_free_gb']} GB",
                    f"at {stats['root']}. Recording resumes once space is freed.",
                ],
                always=True,
            )
        elif not self.recorder.disk_paused and self._disk_alerted:
            self._disk_alerted = False
            self.alerts.send("disk-ok", "OFI recorder resumed: disk space recovered", [])

    def heartbeat(self) -> dict[str, Any]:
        """Log and return one summary line (rates are per second since the last one)."""
        now = time.monotonic()
        elapsed = max(now - self._heartbeat_at, 1e-9)
        rates = {
            kind: round((count - self._heartbeat_counts.get(kind, 0)) / elapsed, 2)
            for kind, count in self.counts.items()
            if kind in {"DepthUpdate", "BookSnapshot", "BookTick", "AggTrade", "grid_samples"}
        }
        lag = {
            kind: _percentiles(values)
            for kind, values in self.feed_latency_ms.items()
            if kind in {"depth", "aggTrade"}
        }
        _, details = self.streams.readiness()
        recorder = self.recorder.stats() if self.recorder is not None else None
        line: dict[str, Any] = {
            "uptime_s": round(now - self._started_at),
            "booted": self.booted.is_set(),
            "ready": self.readiness()[0],
            "events_per_s": rates,
            "lag_ms": {k: (v["p50"], v["p99"]) if v else None for k, v in lag.items()},
            "books": {s: b["state"] for s, b in details.get("books", {}).items()},
            "gaps": {s: b["gaps"] for s, b in details.get("books", {}).items()},
            "reconnects": details.get("reconnects"),
            "backlog": details.get("queue_depth"),
            "pauses": {s: sorted(r) for s, r in self.risk.pauses.items() if r},
            "halted": self.risk.halted,
            "handler_errors": self.counts.get("handler_errors", 0),
        }
        if recorder is not None:
            line["recorder"] = {
                "lines_per_s": round((recorder["lines_written"] - self._heartbeat_lines) / elapsed),
                "disk_free_gb": recorder["disk_free_gb"],
                "disk_paused": recorder["disk_paused"],
                "dropped": recorder["dropped"],
                "errors": recorder["errors"],
            }
            self._heartbeat_lines = recorder["lines_written"]
        self._heartbeat_counts = Counter(self.counts)
        self._heartbeat_at = now
        self.last_heartbeat = line
        log_event("ofi_heartbeat", **line)
        return line

    # --- operator actions -------------------------------------------------------

    def kill(self, source: str, detail: str = "") -> bool:
        fired = self.risk.kill(source, detail)
        if fired:
            log_event("ofi_kill_switch", level=logging.CRITICAL, source=source, detail=detail)
            self._would("cancel_all", "*", "kill_switch")
            self._would("flatten", "*", "kill_switch")
            self.alerts.send(
                "kill",
                "OFI KILL SWITCH",
                [f"source: {source}", detail, "Halted until ack."],
                always=True,
            )
        return fired

    def ack(self, source: str) -> tuple[bool, str]:
        if self.settings.kill_file_path.exists():
            return False, f"remove {self.settings.kill_file_path} first"
        ok, message = self.risk.ack(source)
        if ok:
            log_event("ofi_halt_acknowledged", level=logging.WARNING, source=source)
            self.alerts.send("ack", "OFI halt acknowledged", [message, f"by {source}"], always=True)
        return ok, message

    def _would(self, action: str, symbol: str, reason: str) -> None:
        """No order path exists yet; record what it would have done."""
        self.counts[f"would_{action}"] += 1
        log_event(
            "ofi_would_act",
            level=logging.WARNING,
            action=action,
            symbol=symbol,
            reason=reason,
            execution_mode=self.settings.execution_mode.value,
        )

    # --- reads ------------------------------------------------------------------

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        streams_ready, details = self.streams.readiness()
        details["booted"] = self.booted.is_set()
        if self.boot_error:
            details["boot_error"] = self.boot_error
        details["halted"] = self.risk.halted
        return self.booted.is_set() and streams_ready, details

    def status(self) -> dict[str, Any]:
        ready, stream_details = self.readiness()
        return {
            "ready": ready,
            "execution_mode": self.settings.execution_mode.value,
            "streams": stream_details,
            "risk": self.risk.snapshot(),
            "gate": self.decisions,
            "recorder": self.recorder.stats() if self.recorder else {"enabled": False},
            "fees": self.fees,
            "key_check": self.key_check,
            "heartbeat": self.last_heartbeat,
            "counts": dict(self.counts),
            "feed_latency_ms_uncorrected": {
                kind: _percentiles(values) for kind, values in self.feed_latency_ms.items()
            },
            "alerts": {"recent": self.alerts.sent[-10:], "suppressed": self.alerts.suppressed},
        }
