"""Best bid and ask from one venue's ``<s>@bookTicker`` stream.

Binance demo trading runs its own matching engine, so its book is not
mainnet's. A strategy whose signals come from mainnet still has to price
demo orders off demo's touch, or post-only orders cross or rest far away.
This is that touch, and nothing else: no depth, no trades, no recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ta_core.logging_config import log_event

from .streams import BookTick, WsConnect, _websocket, parse_frame

__all__ = ["Quote", "TouchQuotes"]


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float
    ask: float
    event_ms: int
    recv_ns: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class TouchQuotes:
    def __init__(
        self,
        ws_url: str,
        symbols: Sequence[str],
        *,
        ws_connect: WsConnect | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        max_backoff_seconds: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.symbols = [s.upper() for s in symbols]
        self._ws_connect = ws_connect or _websocket
        self._clock_ns = clock_ns
        self._max_backoff = max_backoff_seconds
        self._sleep = sleep
        self.quotes: dict[str, Quote] = {}
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        streams = "/".join(f"{s.lower()}@bookTicker" for s in self.symbols)
        return f"{self.ws_url}/public/stream?streams={streams}"

    def get(self, symbol: str, max_age_ms: float) -> Quote | None:
        """The latest quote, or None if there is none this fresh."""
        quote = self.quotes.get(symbol)
        if quote is None or not self.connected:
            return None
        if (self._clock_ns() - quote.recv_ns) / 1e6 > max_age_ms:
            return None
        return quote

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="binance-futures-quotes")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False

    async def _run(self) -> None:
        backoff = min(1.0, self._max_backoff)
        while True:
            try:
                async with self._ws_connect(self.url) as socket:
                    self.connected = True
                    self.last_error = None
                    backoff = min(1.0, self._max_backoff)
                    async for message in socket:
                        event = parse_frame(message, self._clock_ns())
                        if isinstance(event, BookTick) and event.bid > 0 and event.ask > 0:
                            self.quotes[event.symbol] = Quote(
                                event.bid, event.ask, event.event_ms, event.recv_ns
                            )
                        elif event == "shutdown":
                            raise ConnectionError("serverShutdown")
                raise ConnectionError("quote stream closed")
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.connected = False
                self.reconnects += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log_event(
                    "binance_futures_quotes_reconnecting",
                    level=logging.WARNING,
                    url=self.ws_url,
                    error=self.last_error,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)

    def status(self) -> dict[str, object]:
        return {
            "url": self.ws_url,
            "connected": self.connected,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "symbols": {s: {"bid": q.bid, "ask": q.ask} for s, q in self.quotes.items()},
        }
