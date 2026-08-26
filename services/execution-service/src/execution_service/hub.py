"""Fan-out from one broker connection to N stream subscribers.

Every method here is synchronous and non-blocking by design. The publish path is
called from the protocol reader loop, so if it could await — on a full queue, a
paused TCP window, a wedged HTTP client — one slow SSE consumer would stall the
tick stream for everybody. That constraint is the entire reason this object
exists separately from the session.

Subscribers are owned here rather than by the session, which is what lets a
reconnect swap the underlying connection without dropping anyone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from ta_contracts import Tick

from .logging_config import log_event

ConnectionState = Literal["starting", "connected", "reconnecting", "stopped"]


@dataclass
class StreamEvent:
    name: str
    payload: dict[str, Any]


@dataclass(eq=False)
class Subscriber:
    """One live stream connection.

    `symbols=None` means every symbol. `dropped` counts ticks discarded because
    this consumer could not keep up.

    eq=False keeps identity hashing: two subscribers with the same filter are
    still distinct connections, and the hub stores them in a set.
    """

    queue: asyncio.Queue[StreamEvent]
    symbols: frozenset[str] | None = None
    dropped: int = 0
    _warned: bool = field(default=False, repr=False)


class MarketDataHub:
    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[Subscriber] = set()
        self._quotes: dict[str, Tick] = {}
        self._state: ConnectionState = "starting"
        self._last_error: str | None = None

    # --- subscriber lifecycle -----------------------------------------------

    def register(self, symbols: frozenset[str] | None = None) -> Subscriber:
        subscriber = Subscriber(queue=asyncio.Queue(maxsize=self._queue_size), symbols=symbols)
        self._subscribers.add(subscriber)
        return subscriber

    def unregister(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- publish ------------------------------------------------------------

    def publish_tick(self, tick: Tick) -> None:
        self._quotes[tick.symbol] = tick
        event = StreamEvent("tick", tick.model_dump(mode="json"))
        # Iterate a copy: a generator may unregister itself while we publish.
        for subscriber in tuple(self._subscribers):
            if subscriber.symbols is not None and tick.symbol not in subscriber.symbols:
                continue
            self._offer(subscriber, event)

    def publish_status(self, state: ConnectionState, *, error: str | None = None) -> None:
        self._state = state
        self._last_error = error
        for subscriber in tuple(self._subscribers):
            self._offer(subscriber, StreamEvent("status", self._status_payload(subscriber)))

    def _offer(self, subscriber: Subscriber, event: StreamEvent) -> None:
        try:
            subscriber.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        # Drop the OLDEST. For quotes the newest supersedes the stale one, so a
        # lagging consumer should catch up to now rather than replay a backlog.
        with contextlib.suppress(asyncio.QueueEmpty):
            subscriber.queue.get_nowait()
        subscriber.dropped += 1
        if not subscriber._warned:
            subscriber._warned = True
            log_event(
                "stream_subscriber_lagging",
                level=logging.WARNING,
                console=False,
                queue_size=self._queue_size,
                symbols=sorted(subscriber.symbols) if subscriber.symbols else None,
            )
        with contextlib.suppress(asyncio.QueueFull):
            subscriber.queue.put_nowait(event)

    # --- reads --------------------------------------------------------------

    def status_event(self, subscriber: Subscriber) -> StreamEvent:
        return StreamEvent("status", self._status_payload(subscriber))

    def _status_payload(self, subscriber: Subscriber) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": self._state, "dropped": subscriber.dropped}
        if self._last_error:
            payload["error"] = self._last_error
        return payload

    @property
    def state(self) -> ConnectionState:
        return self._state

    def last_tick(self, symbol: str) -> Tick | None:
        return self._quotes.get(symbol)

    def known_symbols(self) -> frozenset[str]:
        return frozenset(self._quotes)

    def newest_tick_age_seconds(self, now: Any) -> float | None:
        if not self._quotes:
            return None
        newest = max(tick.ts for tick in self._quotes.values())
        return (now - newest).total_seconds()

    def snapshot(self, now: Any) -> dict[str, Any]:
        return {
            "state": self._state,
            "subscribers": len(self._subscribers),
            "symbols_with_quotes": len(self._quotes),
            "newest_tick_age_seconds": self.newest_tick_age_seconds(now),
            "last_error": self._last_error,
        }
