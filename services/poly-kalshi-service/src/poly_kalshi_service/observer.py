"""The observer: statistics in a process of their own, off the trader's event loop.

The trader (``poly-kalshi-service``) keeps only what an order depends on: the
feeds, the books, the live rule, the paper venue and the ledger
(``Engine(role="trade")``). Everything else runs here, on the same lines, in a
child process the trader starts and supervises:

- the tape (``Recorder``, with its writer thread and ``gzip`` child);
- the journal, the only writer of ``<state_dir>/journal`` (the trader's
  ``decision``, ``fill`` and ``settle`` records come over the pipe, in order);
- ``Engine(role="observe")``: shadow variants, markouts, the per-second series,
  the uncensored gap, ``window`` and ``resolution`` records, BBO cross-checks and
  feed-latency samples;
- PolyBolt reference prices (venue ``b``), when CLOB API credentials are set;
- a loopback HTTP API (``PK_OBSERVER_PORT``) that ``pk-status`` reads.

**The pipe.** The trader stamps every line exactly as before and appends
``"<recv_ns> <venue> <frame>\\n"``, the tape's own format, to a buffer flushed
once per loop iteration to the child's stdin through a non-blocking asyncio
transport. The trader never waits on the observer: if the child falls more than
``max_buffer_bytes`` behind, market lines are dropped (counted, and a ``gap``
noted on the tape) while journal records are always kept. Internal messages
use venue ``x`` and never reach the tape: ``{"k": "j", "r": record}`` (a journal
record) and ``{"k": "gap", "lines": n}``.

**Lifecycle.** The child runs in its own session (a Ctrl-C to the trader does
not reach it). On the trader's stop the pipe is closed; the child drains it,
closes the hour file and its manifest, and exits. If the trader dies, the child
sees EOF and does the same. If the child dies, the trader writes its journal
records directly (``fallback``), alerts, and starts a new child on the next
health tick; the tape then has a gap and a new session file.

PolyBolt lines are stamped by the observer's clock, which is the same host
clock as the trader's, but they are written as they arrive here, so in the file
they can sit a few ms out of ``recv_ns`` order relative to the market lines,
which come through the pipe. Research that joins them sorts on ``recv_ns``. The
engine ignores venue ``b``, so replay is unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

import orjson
from ta_core.logging_config import log_event

from .trades import JsonlDaily

__all__ = ["Observer", "ObserverLink", "observer_argv", "run"]

NS = 1_000_000_000
MAX_BUFFER_BYTES = 64 * 2**20
READ_CHUNK = 1 << 20
STOP_TIMEOUT_S = 25.0  # inside the unit's TimeoutStopSec
LOGGER_NAME = "poly_kalshi_service.observer"


def observer_argv(profile: str | None) -> list[str]:
    argv = [sys.executable, "-m", "poly_kalshi_service.observer"]
    if profile:
        argv += ["--profile", profile]
    return argv


# --- the trader's side ---------------------------------------------------------


class _JournalSink:
    """What the trader's engine writes its records to."""

    def __init__(self, link: ObserverLink) -> None:
        self._link = link

    def write(self, t_ns: int, record: dict[str, Any]) -> None:
        self._link.journal(t_ns, record)


class ObserverLink:
    def __init__(
        self,
        argv: list[str],
        *,
        fallback: JsonlDaily,
        max_buffer_bytes: int = MAX_BUFFER_BYTES,
        on_down: Callable[[str], None] | None = None,
    ) -> None:
        self.argv = argv
        self.fallback = fallback
        self.max_buffer_bytes = max_buffer_bytes
        self._on_down = on_down
        self.proc: asyncio.subprocess.Process | None = None
        self._transport: asyncio.WriteTransport | None = None
        self._buf: list[str] = []
        self._scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.spawns = 0
        self.lines_sent = 0
        self.dropped = 0
        self.fallback_writes = 0
        self.last_exit: int | None = None
        self._gap_lines = 0
        self.sink = _JournalSink(self)

    # --- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._spawn()

    async def _spawn(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            start_new_session=True,  # a Ctrl-C to the trader must not cut the tape
        )
        assert self.proc.stdin is not None
        self._transport = self.proc.stdin.transport  # type: ignore[assignment]
        self.spawns += 1
        log_event("observer_started", pid=self.proc.pid, n=self.spawns)
        if self.spawns > 1:
            self._gap_lines += 1  # the lines while it was down are not on the tape

    @property
    def alive(self) -> bool:
        return (
            self.proc is not None
            and self.proc.returncode is None
            and self._transport is not None
            and not self._transport.is_closing()
        )

    async def check(self) -> None:
        """Health tick: start a new child if this one died."""
        if self.proc is None or self.alive:
            return
        self.last_exit = self.proc.returncode
        log_event("observer_down", level=logging.ERROR, returncode=self.last_exit)
        if self._on_down is not None:
            self._on_down(f"returncode={self.last_exit}")
        self.proc = None
        self._transport = None
        await self._spawn()

    async def close(self) -> None:
        self._flush()
        proc, transport = self.proc, self._transport
        self.proc = self._transport = None
        if proc is None:
            return
        if transport is not None and not transport.is_closing():
            transport.close()  # EOF: the child drains, closes the tape and exits
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT_S)
        except TimeoutError:
            log_event("observer_stop_timeout", level=logging.ERROR, pid=proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    # --- the hot path -------------------------------------------------------------

    def line(self, recv_ns: int, venue: str, text: str) -> None:
        """One stamped line. A list append; the write happens once per loop turn."""
        self._buf.append(f"{recv_ns} {venue} {text}\n")
        if not self._scheduled:
            self._schedule()

    def journal(self, t_ns: int, record: dict[str, Any]) -> None:
        if not self.alive:
            self.fallback_writes += 1
            self.fallback.write(t_ns, record)
            return
        body = orjson.dumps({"k": "j", "r": record}, default=str).decode()
        self._buf.append(f"{t_ns} x {body}\n")
        if not self._scheduled:
            self._schedule()

    def _schedule(self) -> None:
        self._scheduled = True
        loop = self._loop
        if loop is None:
            try:
                loop = self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._flush()  # no loop (tests): write now
                return
        loop.call_soon(self._flush)

    def _flush(self) -> None:
        self._scheduled = False
        if not self._buf:
            return
        lines, self._buf = self._buf, []
        transport = self._transport
        if not self.alive or transport is None:
            # Journal records go to the fallback; market lines are lost to the tape.
            for line in lines:
                if line.split(" ", 2)[1] == "x":
                    t_ns, _x, body = line.split(" ", 2)
                    self.fallback_writes += 1
                    self.fallback.write(int(t_ns), orjson.loads(body)["r"])
                else:
                    self.dropped += 1
            return
        if transport.get_write_buffer_size() > self.max_buffer_bytes:
            kept = [line for line in lines if line.split(" ", 2)[1] == "x"]
            self.dropped += len(lines) - len(kept)
            self._gap_lines += len(lines) - len(kept)
            lines = kept
            if not lines:
                return
        elif self._gap_lines:
            gap = orjson.dumps({"k": "gap", "lines": self._gap_lines}).decode()
            lines.insert(0, f"{time.time_ns()} x {gap}\n")
            self._gap_lines = 0
        transport.write("".join(lines).encode())
        self.lines_sent += len(lines)

    def stats(self) -> dict[str, Any]:
        transport = self._transport
        return {
            "enabled": True,
            "alive": self.alive,
            "pid": None if self.proc is None else self.proc.pid,
            "spawns": self.spawns,
            "lines_sent": self.lines_sent,
            "dropped": self.dropped,
            "buffer_bytes": 0 if transport is None else transport.get_write_buffer_size(),
            "fallback_writes": self.fallback_writes,
            "last_exit": self.last_exit,
        }


# --- the observer's side -------------------------------------------------------


class Observer:
    """Lines in (from the pipe), tape and statistics out. No I/O of its own."""

    def __init__(self, engine: Any, journal: JsonlDaily, recorder: Any | None = None) -> None:
        self.engine = engine
        self.journal = journal
        self.recorder = recorder
        self.lines = 0
        self.records = 0
        self.gaps = 0
        self.bad_lines = 0
        self.engine_errors = 0
        self.last_ns = 0
        self.started_ns = time.time_ns()

    def handle(self, line: str) -> None:
        try:
            ns, venue, text = line.rstrip("\n").split(" ", 2)
            recv_ns = int(ns)
        except ValueError:
            self.bad_lines += 1
            return
        if venue == "x":
            self._internal(recv_ns, text)
            return
        self.lines += 1
        self.last_ns = recv_ns
        if self.recorder is not None:
            self.recorder.write(venue, recv_ns, text)
        try:
            self.engine.handle_line(recv_ns, venue, text)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the statistics
            self.engine_errors += 1
            log_event("observer_engine_error", level=logging.ERROR, error=repr(exc)[:200])

    def _internal(self, t_ns: int, text: str) -> None:
        try:
            body = orjson.loads(text)
        except ValueError:
            self.bad_lines += 1
            return
        kind = body.get("k")
        if kind == "j":
            record = body["r"]
            self.records += 1
            self.journal.write(t_ns, record)
            self.engine.note_fill(record)
        elif kind == "gap":
            self.gaps += 1
            if self.recorder is not None:
                self.recorder.note_gap()
            log_event("observer_gap", level=logging.WARNING, lines=body.get("lines"))

    def status(self) -> dict[str, Any]:
        now = time.time_ns()
        return {
            "lines": self.lines,
            "journal_records": self.records,
            "gaps": self.gaps,
            "bad_lines": self.bad_lines,
            "engine_errors": self.engine_errors,
            # How far the observer runs behind the trader's clock.
            "behind_ms": None if not self.last_ns else round((now - self.last_ns) / 1e6, 1),
            "started_ns": self.started_ns,
            "engine": {
                "counters": dict(self.engine.counters),
                "latency": self.engine.metrics.snapshot(),
                "markouts_open": len(self.engine.markouts),
            },
            "recorder": self.recorder.stats() if self.recorder else {"enabled": False},
        }


async def _read_stdin(observer: Observer) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=READ_CHUNK * 16)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tail = b""
    while True:
        chunk = await reader.read(READ_CHUNK)
        if not chunk:
            break
        data = tail + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            tail = data
            continue
        tail = data[cut + 1 :]
        for line in data[: cut + 1].decode("utf-8", "replace").splitlines():
            if line:
                observer.handle(line)
    if tail:
        observer.handle(tail.decode("utf-8", "replace"))


def build_observer(settings: Any) -> tuple[Observer, Any]:
    """The observer's engine, journal, recorder and PolyBolt feed, from the settings."""
    from .build import build_id, config_hash
    from .engine import Engine
    from .feeds import PolyBoltFeed
    from .ledger import Ledger
    from .recorder import Recorder
    from .risk import RiskLimits, RiskState
    from .shadow import Shadow

    engine = Engine(
        settings.params,
        settings.assets,
        # In memory only: the trader's ledger is the one that counts.
        ledger=Ledger(start_cash=settings.paper_start_cash_usd, cash=settings.paper_start_cash_usd),
        risk=RiskState(
            RiskLimits(
                max_open_notional_usd=settings.max_open_notional_usd,
                daily_loss_limit_usd=settings.daily_loss_limit_usd,
                kill_file=None,
            )
        ),
        venue=None,
        journal=None,
        stamp={
            "epoch": settings.results_epoch,
            "build": build_id(),
            "config": config_hash(
                settings.params,
                settings.paper_latency_ms,
                settings.limit_offset,
                settings.size_to_depth,
            ),
        },
        shadow=Shadow(settings.shadow_variants, settings.params),
        limit_offset=settings.limit_offset,
        role="observe",
    )
    journal = JsonlDaily(settings.state_dir / "journal")
    engine.journal = journal
    recorder = (
        Recorder(
            settings.record_dir,
            host_tag=settings.host_tag,
            min_free_bytes=int(settings.min_free_disk_gb * 1e9),
        )
        if settings.record_enabled
        else None
    )
    observer = Observer(engine, journal, recorder)
    polybolt = None
    credentials = settings.polybolt_credentials
    if credentials is not None and recorder is not None:

        def emit(venue: str, text: str) -> None:
            if "\n" in text or "\r" in text:
                text = text.replace("\r", " ").replace("\n", " ").strip()
            recorder.write(venue, time.time_ns(), text)

        polybolt = PolyBoltFeed(settings.polybolt_url, emit, credentials, settings.assets)
    return observer, polybolt


def create_observer_app(settings: Any, observer: Observer, polybolt: Any) -> Any:
    from fastapi import Depends, Query
    from fastapi.responses import Response
    from ta_core import create_base_app

    app, authenticate = create_base_app(settings, title="poly-kalshi-observer")
    auth = [Depends(authenticate)]

    @app.get("/v1/obs/status", dependencies=auth)
    async def status() -> Response:
        body = observer.status()
        body["polybolt"] = polybolt.stats() if polybolt is not None else {"enabled": False}
        return Response(orjson.dumps(body, default=str), media_type="application/json")

    @app.get("/v1/series", dependencies=auth)
    async def series(since: int = Query(default=0, ge=0)) -> Response:
        body = {"points": observer.engine.series_since(since)}
        return Response(orjson.dumps(body, default=str), media_type="application/json")

    return app


async def amain(profile: str | None) -> None:
    import uvicorn
    from ta_core.logging_config import configure_file_logs, configure_logging

    from .config import load_settings

    settings = load_settings(profile)
    configure_logging(settings.log_level, name=LOGGER_NAME)
    configure_file_logs(settings.events_log_path)
    observer, polybolt = build_observer(settings)
    if observer.recorder is not None:
        observer.recorder.start()
    tasks: list[asyncio.Task[Any]] = []
    if polybolt is not None:
        tasks.append(asyncio.create_task(polybolt.run(), name="pk-polybolt"))
    server = uvicorn.Server(
        uvicorn.Config(
            create_observer_app(settings, observer, polybolt),
            host="127.0.0.1",
            port=settings.observer_port,
            log_level="warning",
            access_log=False,
        )
    )
    tasks.append(asyncio.create_task(server.serve(), name="pk-observer-http"))
    log_event("observer_running", port=settings.observer_port, polybolt=polybolt is not None)
    try:
        await _read_stdin(observer)  # until the trader closes the pipe
    finally:
        server.should_exit = True
        for task in tasks:
            if task.get_name() != "pk-observer-http":
                task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5)
        observer.journal.close()
        if observer.recorder is not None:
            observer.recorder.stop(timeout=STOP_TIMEOUT_S - 5)
        log_event("observer_stopped", lines=observer.lines, records=observer.records)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pk-observer")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    try:
        import uvloop

        uvloop.run(amain(args.profile))
    except ImportError:
        asyncio.run(amain(args.profile))


if __name__ == "__main__":
    run()
