"""Typed USDⓈ-M market streams with a verified local book per symbol.

Since the 2026-03 routing split, Binance serves futures market data on two
entry points, so this opens two combined-stream connections:

- ``/public``: the book stream and, optionally, ``<s>@bookTicker``
- ``/market``: ``<s>@aggTrade`` (optional), ``<s>@markPrice@1s``, ``<s>@forceOrder``

The book comes one of two ways (``BINANCE_FUTURES_BOOK_MODE``):

- ``diff``: ``<s>@depth@<speed>`` diffs kept in step by ``DepthSync`` with REST
  snapshots. The full book, but it needs a link that keeps up with the diff
  stream; a lagging link means gaps and resyncs.
- ``partial``: ``<s>@depth<N>@<speed>`` complete top-N snapshots through
  ``SnapshotSync``. Stateless and a few KB/s; a slow link makes the book older,
  never wrong. Binance labels these frames ``depthUpdate`` too, so they are
  told apart by stream name and arrive as ``BookSnapshot`` events.

Every frame is stamped with ``recv_ns`` (local wall clock, ``time.time_ns``)
the moment it is read, and handed raw to ``on_raw`` before any parsing, so a
recorder sees exactly the bytes and timestamps the features see.

Ordering is the point of the design. Readers only parse and enqueue; the
consumer of :meth:`FuturesStreams.events` applies depth diffs to the books as
it dequeues them. So when a consumer holds a ``DepthUpdate`` the book reflects
exactly the events it has seen, never ones still queued behind it. Snapshots
and reconnect resets travel through the same queue for the same reason.

The plugin never sends anything to Binance but GETs and subscriptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal

from ta_contracts import InstrumentInfo
from ta_core import ServiceError
from ta_core.logging_config import log_event

from .depth import DepthDiff, DepthSync, SnapshotSync, SyncOutcome, SyncState
from .instruments import load_instruments
from .rest import FapiRest, depth_weight

__all__ = [
    "AggTrade",
    "BookSnapshot",
    "BookStatus",
    "BookTick",
    "Channel",
    "DepthUpdate",
    "FuturesStreams",
    "Liquidation",
    "MarkPrice",
    "RawSink",
    "StreamEvent",
    "StreamStatus",
    "WsConnect",
    "parse_frame",
]

Channel = Literal["public", "market"]
WsConnect = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]
# (channel, recv_ns, raw text). Snapshots arrive with channel "snapshot".
RawSink = Callable[[str, int, str], None]

QUEUE_LIMIT = 200_000
# <s>@depth5@100ms, <s>@depth10, <s>@depth20@500ms: partial (snapshot) streams.
_PARTIAL_STREAM = re.compile(r"@depth(5|10|20)(@|$)")


@dataclass(frozen=True, slots=True)
class DepthUpdate:
    symbol: str
    event_ms: int
    txn_ms: int
    first_id: int
    final_id: int
    prev_final_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    recv_ns: int
    # Set by the consumer side once the diff has been offered to the book.
    outcome: SyncOutcome | None = None


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """A complete top-N book from a partial-depth stream."""

    symbol: str
    event_ms: int
    txn_ms: int
    final_id: int
    prev_final_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    recv_ns: int
    outcome: SyncOutcome | None = None


@dataclass(frozen=True, slots=True)
class BookTick:
    symbol: str
    update_id: int
    event_ms: int
    txn_ms: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    recv_ns: int


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    trade_id: int
    price: float
    qty: float
    trade_ms: int
    event_ms: int
    # True when the buyer was the maker, i.e. the aggressor sold.
    buyer_is_maker: bool
    recv_ns: int


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    mark: float
    index: float
    funding_rate: float
    next_funding_ms: int
    event_ms: int
    recv_ns: int


@dataclass(frozen=True, slots=True)
class Liquidation:
    symbol: str
    side: str  # BUY or SELL: the liquidation order's side
    price: float
    avg_price: float
    qty: float
    trade_ms: int
    event_ms: int
    recv_ns: int


@dataclass(frozen=True, slots=True)
class StreamStatus:
    channel: str
    state: Literal["connected", "reconnecting", "server_shutdown", "overflow"]
    recv_ns: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BookStatus:
    symbol: str
    state: SyncState
    reason: str
    recv_ns: int
    gaps: int
    resyncs: int


StreamEvent = (
    DepthUpdate
    | BookSnapshot
    | BookTick
    | AggTrade
    | MarkPrice
    | Liquidation
    | StreamStatus
    | BookStatus
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    symbol: str
    last_update_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    recv_ns: int


@dataclass(frozen=True, slots=True)
class _ResetBooks:
    reason: str
    recv_ns: int


def _levels(rows: Any) -> tuple[tuple[float, float], ...]:
    return tuple((float(price), float(qty)) for price, qty in rows)


def parse_frame(text: str | bytes, recv_ns: int) -> StreamEvent | Literal["shutdown"] | None:
    """One combined-stream frame -> a typed event, ``"shutdown"``, or None."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    kind = data.get("e")
    try:
        if kind == "depthUpdate" and _PARTIAL_STREAM.search(str(payload.get("stream", ""))):
            return BookSnapshot(
                symbol=data["s"],
                event_ms=int(data["E"]),
                txn_ms=int(data["T"]),
                final_id=int(data["u"]),
                prev_final_id=int(data["pu"]),
                bids=_levels(data["b"]),
                asks=_levels(data["a"]),
                recv_ns=recv_ns,
            )
        if kind == "depthUpdate":
            return DepthUpdate(
                symbol=data["s"],
                event_ms=int(data["E"]),
                txn_ms=int(data["T"]),
                first_id=int(data["U"]),
                final_id=int(data["u"]),
                prev_final_id=int(data["pu"]),
                bids=_levels(data["b"]),
                asks=_levels(data["a"]),
                recv_ns=recv_ns,
            )
        if kind == "bookTicker":
            return BookTick(
                symbol=data["s"],
                update_id=int(data["u"]),
                event_ms=int(data["E"]),
                txn_ms=int(data["T"]),
                bid=float(data["b"]),
                bid_qty=float(data["B"]),
                ask=float(data["a"]),
                ask_qty=float(data["A"]),
                recv_ns=recv_ns,
            )
        if kind == "aggTrade":
            return AggTrade(
                symbol=data["s"],
                trade_id=int(data["a"]),
                price=float(data["p"]),
                qty=float(data["q"]),
                trade_ms=int(data["T"]),
                event_ms=int(data["E"]),
                buyer_is_maker=bool(data["m"]),
                recv_ns=recv_ns,
            )
        if kind == "markPriceUpdate":
            return MarkPrice(
                symbol=data["s"],
                mark=float(data["p"]),
                index=float(data["i"]),
                funding_rate=float(data["r"] or 0),
                next_funding_ms=int(data["T"]),
                event_ms=int(data["E"]),
                recv_ns=recv_ns,
            )
        if kind == "forceOrder":
            order = data["o"]
            return Liquidation(
                symbol=order["s"],
                side=order["S"],
                price=float(order["p"]),
                avg_price=float(order["ap"]),
                qty=float(order["z"] or order["q"]),
                trade_ms=int(order["T"]),
                event_ms=int(data["E"]),
                recv_ns=recv_ns,
            )
        if kind == "serverShutdown":
            return "shutdown"
    except (KeyError, TypeError, ValueError):
        return None
    return None


@contextlib.asynccontextmanager
async def _websocket(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
    from websockets.asyncio.client import connect

    # max_queue bounds memory if the reader stalls; the 0ms depth stream for
    # two symbols is a few hundred frames a second at peak.
    async with connect(url, ping_interval=20, ping_timeout=20, max_queue=4096) as socket:
        yield socket


class FuturesStreams:
    """Owns the two connections, the per-symbol books and the event queue."""

    def __init__(
        self,
        settings: Any,
        *,
        rest: FapiRest | None = None,
        ws_connect: WsConnect | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        on_raw: RawSink | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        symbols: tuple[str, ...] = settings.binance_futures_symbols
        if not symbols:
            raise ValueError("Binance futures streams need BINANCE_FUTURES_SYMBOLS")
        self._settings = settings
        self.symbols = symbols
        self._rest = rest or FapiRest(settings)
        self._owns_rest = rest is None
        self._ws_connect = ws_connect or _websocket
        self._clock_ns = clock_ns
        self._sleep = sleep
        self.on_raw = on_raw
        self.mode: Literal["diff", "partial"] = getattr(
            settings, "binance_futures_book_mode", "diff"
        )
        self.syncs: dict[str, DepthSync | SnapshotSync] = {
            symbol: SnapshotSync(symbol) if self.mode == "partial" else DepthSync(symbol)
            for symbol in symbols
        }
        self.instruments: dict[str, InstrumentInfo] = {}
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._tasks: list[asyncio.Task[None]] = []
        self._fetching: dict[str, asyncio.Task[None]] = {}
        self.connected: dict[str, bool] = {"public": False, "market": False}
        self.reconnects: dict[str, int] = {"public": 0, "market": 0}
        self.last_error: str | None = None
        self.overflows = 0

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Load contract specs, then open both channels. Raises on bad symbols."""
        self.instruments = await load_instruments(self._rest, self.symbols)
        self._tasks = [
            asyncio.create_task(self._channel("public"), name="binance-futures-public"),
            asyncio.create_task(self._channel("market"), name="binance-futures-market"),
        ]

    async def close(self) -> None:
        for task in [*self._tasks, *self._fetching.values()]:
            task.cancel()
        for task in [*self._tasks, *self._fetching.values()]:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._fetching.clear()
        if self._owns_rest:
            await self._rest.aclose()

    @property
    def backlog(self) -> int:
        """Events received but not yet consumed."""
        return self._queue.qsize()

    @property
    def ready(self) -> bool:
        return all(self.connected.values()) and all(sync.verified for sync in self.syncs.values())

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        details: dict[str, Any] = {
            "connected": dict(self.connected),
            "reconnects": dict(self.reconnects),
            "books": {
                symbol: {
                    "state": sync.state.value,
                    "gaps": sync.gaps,
                    "resyncs": sync.resyncs,
                    "last_update_id": sync.last_final_id,
                    **({"skipped": sync.skipped} if isinstance(sync, SnapshotSync) else {}),
                }
                for symbol, sync in self.syncs.items()
            },
            "book_mode": self.mode,
            "request_weight_used": self._rest.limiter.used,
            "queue_depth": self._queue.qsize(),
            "overflows": self.overflows,
        }
        if self.last_error:
            details["last_error"] = self.last_error
        return self.ready, details

    # --- URLs ---------------------------------------------------------------

    def stream_names(self, channel: Channel) -> list[str]:
        settings = self._settings
        partial = self.mode == "partial"
        book_ticker = getattr(settings, "binance_futures_book_ticker", None)
        if book_ticker is None:
            book_ticker = not partial
        agg_trades = getattr(settings, "binance_futures_agg_trades", True)
        names: list[str] = []
        for symbol in self.symbols:
            s = symbol.lower()
            if channel == "public":
                if partial:
                    levels = settings.binance_futures_partial_levels
                    names.append(f"{s}@depth{levels}@{settings.binance_futures_partial_speed}")
                else:
                    names.append(f"{s}@depth@{settings.binance_futures_depth_speed}")
                if book_ticker:
                    names.append(f"{s}@bookTicker")
            else:
                if agg_trades:
                    names.append(f"{s}@aggTrade")
                names += [f"{s}@markPrice@1s", f"{s}@forceOrder"]
        return names

    def stream_url(self, channel: Channel) -> str:
        root = self._settings.binance_futures_ws_url.rstrip("/")
        return f"{root}/{channel}/stream?streams={'/'.join(self.stream_names(channel))}"

    # --- consumer side --------------------------------------------------------

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Every event in arrival order, with depth applied to the books.

        One consumer only: the books advance as this iterator advances.
        """
        while True:
            item = await self._queue.get()
            for event in self._process(item):
                yield event

    def _process(self, item: Any) -> list[StreamEvent]:
        if isinstance(item, BookSnapshot):
            sync = self.syncs.get(item.symbol)
            if not isinstance(sync, SnapshotSync):
                return []  # a partial frame on a diff-mode book: not ours to apply
            before = sync.state
            outcome = sync.on_snapshot(item.final_id, item.prev_final_id, item.bids, item.asks)
            out: list[StreamEvent] = [_snapshot_outcome(item, outcome)]
            if outcome is SyncOutcome.CROSSED:
                out.append(self._book_status(sync, "crossed", item.recv_ns))
            elif sync.state is not before:
                out.append(self._book_status(sync, "snapshot", item.recv_ns))
            return out
        if isinstance(item, DepthUpdate):
            sync = self.syncs.get(item.symbol)
            if not isinstance(sync, DepthSync):
                return []
            before = sync.state
            outcome = sync.on_diff(
                DepthDiff(item.first_id, item.final_id, item.prev_final_id, item.bids, item.asks)
            )
            out = [_with_outcome(item, outcome)]
            if outcome in {SyncOutcome.GAP, SyncOutcome.CROSSED}:
                log_event(
                    "binance_futures_book_gap",
                    level=logging.WARNING,
                    symbol=item.symbol,
                    outcome=outcome.value,
                    gaps=sync.gaps,
                )
                out.append(self._book_status(sync, outcome.value, item.recv_ns))
            elif sync.state is not before:
                out.append(self._book_status(sync, "bridged", item.recv_ns))
            self._maybe_fetch(sync)
            return out
        if isinstance(item, _Snapshot):
            sync = self.syncs[item.symbol]
            assert isinstance(sync, DepthSync)
            self._fetching.pop(item.symbol, None)
            outcome = sync.apply_snapshot(item.last_update_id, item.bids, item.asks)
            reason = "snapshot" if outcome is None else f"snapshot_{outcome.value}"
            self._maybe_fetch(sync)
            return [self._book_status(sync, reason, item.recv_ns)]
        if isinstance(item, _ResetBooks):
            for task in self._fetching.values():
                task.cancel()
            self._fetching.clear()
            out = []
            for sync in self.syncs.values():
                sync.reset()
                out.append(self._book_status(sync, item.reason, item.recv_ns))
            return out
        return [item]

    def _book_status(self, sync: DepthSync | SnapshotSync, reason: str, recv_ns: int) -> BookStatus:
        return BookStatus(sync.symbol, sync.state, reason, recv_ns, sync.gaps, sync.resyncs)

    def _maybe_fetch(self, sync: DepthSync | SnapshotSync) -> None:
        """Ask for a snapshot once a diff is buffered, never two at once."""
        if (
            isinstance(sync, DepthSync)
            and sync.state is SyncState.SYNCING
            and sync.needs_snapshot
            and sync.symbol not in self._fetching
            and self.connected["public"]
        ):
            self._fetching[sync.symbol] = asyncio.create_task(
                self._fetch_snapshot(sync.symbol), name=f"depth-snapshot-{sync.symbol}"
            )

    async def _fetch_snapshot(self, symbol: str) -> None:
        limit = self._settings.binance_futures_depth_snapshot_limit
        delay = 0.5
        while True:
            try:
                payload = await self._rest.get(
                    "/fapi/v1/depth",
                    {"symbol": symbol, "limit": limit},
                    depth_weight(limit),
                    unavailable="depth_unavailable",
                )
                break
            except ServiceError as exc:
                self.last_error = f"snapshot {symbol}: {exc.message}"
                log_event(
                    "binance_futures_snapshot_failed",
                    level=logging.WARNING,
                    symbol=symbol,
                    error=exc.as_dict(),
                )
                await self._sleep(delay)
                delay = min(delay * 2, 30.0)
        recv_ns = self._clock_ns()
        if self.on_raw is not None:
            raw = json.dumps(
                {"stream": f"{symbol.lower()}@depthSnapshot", "data": payload},
                separators=(",", ":"),
            )
            self.on_raw("snapshot", recv_ns, raw)
        self._enqueue(
            _Snapshot(
                symbol=symbol,
                last_update_id=int(payload["lastUpdateId"]),
                bids=_levels(payload["bids"]),
                asks=_levels(payload["asks"]),
                recv_ns=recv_ns,
            )
        )

    # --- reader side ----------------------------------------------------------

    def _enqueue(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # The consumer is hopelessly behind. Dropping a diff would corrupt
            # the book silently, so drop everything and resync loudly instead.
            self.overflows += 1
            while not self._queue.empty():
                self._queue.get_nowait()
            now = self._clock_ns()
            self._queue.put_nowait(StreamStatus("all", "overflow", now))
            self._queue.put_nowait(_ResetBooks("overflow", now))
            log_event("binance_futures_queue_overflow", level=logging.ERROR)

    async def _channel(self, channel: Channel) -> None:
        ceiling = self._settings.binance_futures_reconnect_max_backoff_seconds
        backoff = min(1.0, ceiling)
        while True:
            try:
                async with self._ws_connect(self.stream_url(channel)) as socket:
                    self.connected[channel] = True
                    self.last_error = None
                    backoff = min(1.0, ceiling)
                    now = self._clock_ns()
                    if channel == "public":
                        # Diffs were missed while disconnected: every book restarts.
                        self._enqueue(_ResetBooks("reconnected", now))
                    self._enqueue(StreamStatus(channel, "connected", now))
                    async for message in socket:
                        recv_ns = self._clock_ns()
                        text = message.decode() if isinstance(message, bytes) else message
                        if self.on_raw is not None:
                            self.on_raw(channel, recv_ns, text)
                        event = parse_frame(text, recv_ns)
                        if event == "shutdown":
                            self._enqueue(StreamStatus(channel, "server_shutdown", recv_ns))
                            raise ConnectionError("Binance sent serverShutdown")
                        if event is not None:
                            self._enqueue(event)
                raise ConnectionError("Binance closed the stream")
            except asyncio.CancelledError:
                self.connected[channel] = False
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.connected[channel] = False
                self.last_error = f"{channel}: {type(exc).__name__}: {exc}"[:200]
                self.reconnects[channel] += 1
                self._enqueue(
                    StreamStatus(channel, "reconnecting", self._clock_ns(), self.last_error)
                )
                log_event(
                    "binance_futures_stream_reconnecting",
                    level=logging.WARNING,
                    channel=channel,
                    error=self.last_error,
                    backoff_seconds=backoff,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, ceiling)


def _with_outcome(update: DepthUpdate, outcome: SyncOutcome) -> DepthUpdate:
    return DepthUpdate(
        symbol=update.symbol,
        event_ms=update.event_ms,
        txn_ms=update.txn_ms,
        first_id=update.first_id,
        final_id=update.final_id,
        prev_final_id=update.prev_final_id,
        bids=update.bids,
        asks=update.asks,
        recv_ns=update.recv_ns,
        outcome=outcome,
    )


def _snapshot_outcome(snapshot: BookSnapshot, outcome: SyncOutcome) -> BookSnapshot:
    return BookSnapshot(
        symbol=snapshot.symbol,
        event_ms=snapshot.event_ms,
        txn_ms=snapshot.txn_ms,
        final_id=snapshot.final_id,
        prev_final_id=snapshot.prev_final_id,
        bids=snapshot.bids,
        asks=snapshot.asks,
        recv_ns=snapshot.recv_ns,
        outcome=outcome,
    )
