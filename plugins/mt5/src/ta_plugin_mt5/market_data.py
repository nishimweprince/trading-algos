"""One MetaTrader 5 terminal as a ``ta_plugin_api.MarketDataProvider``.

Three things MT5 gets wrong for a consumer, and this module puts right:

- **Server time.** Bars and ticks are stamped in broker-server time, which is
  rarely UTC. Every timestamp is shifted by ``MT5_SERVER_UTC_OFFSET_SECONDS``.
- **Interval start.** ``copy_rates`` stamps a bar at its open; the platform
  contract is the interval *end*, so the duration is added.
- **The forming bar.** The newest row is still open. It is dropped, and one
  extra row is requested so ``count`` closed bars still come back.

MT5 has no push channel into Python, so quotes are polled every
``MT5_QUOTE_POLL_SECONDS`` and published to the hub only when they change. One
terminal is one feed (``None``); a host with two terminals runs two processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from ta_contracts import TIMEFRAME_MINUTES, Candle, InstrumentInfo, MarketQuote, Timeframe
from ta_core import ServiceError
from ta_core.logging_config import log_event
from ta_plugin_api import MarketDataHub, ProviderCapabilities

from .symbols import load_mt5_manifest
from .terminal import ConnectionSnapshot, MT5Adapter, SymbolSnapshot, TickSnapshot

__all__ = ["MT5MarketData"]

Clock = Callable[[], datetime]
OFFSET_TOLERANCE_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MT5MarketData:
    name = "mt5"

    def __init__(
        self,
        adapter: MT5Adapter,
        settings: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        if settings.symbols_file is None:
            raise ValueError("MT5 market data needs SYMBOLS_FILE (canonical to broker symbols)")
        self._adapter = adapter
        self._settings = settings
        self._clock = clock
        self._offset = timedelta(seconds=settings.mt5_server_utc_offset_seconds)
        self._manifest = load_mt5_manifest(settings.symbols_file)
        self._hub = MarketDataHub(queue_size=settings.subscriber_queue_size)
        # The MetaTrader5 package is one global connection; serialize access to it.
        self._lock = threading.Lock()
        self._initialized = asyncio.Event()
        self._connection: ConnectionSnapshot | None = None
        self._poller: asyncio.Task[None] | None = None
        self._offset_checked = False
        self._last_status_error: str | None = None

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        ok = await asyncio.to_thread(self._call, self._adapter.initialize, self._settings)
        if not ok:
            log_event(
                "mt5_initialize_failed",
                level=logging.ERROR,
                last_error=self._safe_last_error(),
            )
            self._set_state("stopped", "MetaTrader 5 initialize() failed")
            return
        self._initialized.set()
        self._set_state("connected")
        self._poller = asyncio.create_task(self._poll_quotes(), name="mt5-quote-poller")

    async def wait_ready(self, timeout_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._initialized.wait(), timeout_seconds)
        return self._initialized.is_set()

    async def close(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
        if self._initialized.is_set():
            await asyncio.to_thread(self._call, self._adapter.shutdown)
        self._initialized.clear()
        self._set_state("stopped")

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        connection = self._connection
        connected = bool(connection and connection.connected)
        details: dict[str, Any] = {
            "initialized": self._initialized.is_set(),
            "connected": connected,
            "symbols": len(self._manifest),
            "server_utc_offset_seconds": int(self._offset.total_seconds()),
        }
        if connection is not None and connection.reason:
            details["reason"] = connection.reason
        return self._initialized.is_set() and connected, details

    def feeds(self) -> frozenset[str | None]:
        return frozenset({None})

    # --- reads --------------------------------------------------------------

    def capabilities(self, feed: str | None) -> ProviderCapabilities:
        self._require_feed(feed)
        known = self._adapter.constants.timeframes
        return ProviderCapabilities(
            timeframes=tuple(tf for tf in Timeframe if tf.value in known),
            streaming=True,
            bid_ask=True,
        )

    def instruments(self, feed: str | None) -> list[InstrumentInfo]:
        self._require_feed(feed)
        self._require_ready()
        instruments = []
        for canonical, broker in self._manifest.items():
            info = self._call(self._adapter.symbol_info, broker)
            if info is None:
                continue
            instruments.append(_instrument(canonical, info))
        return instruments

    def resolve_symbols(self, feed: str | None, symbols: Iterable[str]) -> frozenset[str]:
        self._require_feed(feed)
        self._require_ready()
        requested = frozenset(symbols)
        unknown = sorted(requested - set(self._manifest))
        if unknown:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                f"Unknown instruments {unknown}",
                {"configured": list(self._manifest)},
            )
        return requested

    async def quote(self, feed: str | None, symbol: str) -> MarketQuote:
        self._require_feed(feed)
        self._require_ready()
        broker = self._broker_symbol(symbol)
        tick = await asyncio.to_thread(self._call, self._adapter.symbol_tick, broker)
        quote = self._to_quote(symbol, broker, tick)
        if quote is None:
            raise ServiceError(503, "tick_unavailable", "The terminal returned no valid quote")
        return quote

    async def candles(
        self,
        feed: str | None,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        self._require_feed(feed)
        return await asyncio.to_thread(self._candles_sync, symbol, timeframe, count, to)

    def hub(self, feed: str | None) -> MarketDataHub:
        self._require_feed(feed)
        return self._hub

    # --- candles ------------------------------------------------------------

    def _candles_sync(
        self, symbol: str, timeframe: Timeframe, count: int, to: datetime | None
    ) -> list[Candle]:
        self._require_ready()
        mt5_timeframe = self._adapter.constants.timeframes.get(timeframe.value)
        if mt5_timeframe is None:
            raise ServiceError(
                422,
                "timeframe_not_supported",
                f"MetaTrader 5 does not serve {timeframe.value}",
                {"supported": list(self._adapter.constants.timeframes)},
            )
        broker = self._broker_symbol(symbol)
        self._ensure_selected(broker)
        server_to = None if to is None else int((to + self._offset).timestamp())
        rows = self._call(self._adapter.copy_rates, broker, mt5_timeframe, count + 1, server_to)
        if rows is None:
            raise ServiceError(
                503,
                "candles_unavailable",
                "The terminal did not return candle data",
                {"last_error": self._safe_last_error()},
            )
        duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        cutoff = min(self._clock(), to) if to is not None else self._clock()
        closed = []
        for row in rows:
            end = datetime.fromtimestamp(int(row["time"]), UTC) - self._offset + duration
            if end > cutoff:
                continue  # still forming, or beyond the requested window
            closed.append(
                Candle(
                    ts=end,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    provider=self.name,
                    source_instrument=broker,
                    spread_source="unavailable",
                )
            )
        closed.sort(key=lambda candle: candle.ts)
        return closed[-count:]

    # --- quotes -------------------------------------------------------------

    async def _poll_quotes(self) -> None:
        interval = self._settings.mt5_quote_poll_seconds
        while True:
            try:
                # Terminal reads block, so they run in a thread; publishing touches
                # the hub's asyncio queues, which are only safe on the loop thread.
                reading = await asyncio.to_thread(self._read_terminal)
                self._publish(*reading)
            except Exception as exc:  # noqa: BLE001 - one bad poll must not end the stream
                log_event("mt5_quote_poll_failed", level=logging.WARNING, error=str(exc))
            await asyncio.sleep(interval)

    def _poll_once(self) -> None:
        """One synchronous poll on the calling (loop) thread. For tests."""
        self._publish(*self._read_terminal())

    def _read_terminal(
        self,
    ) -> tuple[ConnectionSnapshot, list[tuple[str, str, TickSnapshot | None]]]:
        connection = self._call(self._adapter.connection_snapshot)
        if not connection.connected:
            return connection, []
        ticks = [
            (canonical, broker, self._call(self._adapter.symbol_tick, broker))
            for canonical, broker in self._manifest.items()
        ]
        return connection, ticks

    def _publish(
        self,
        connection: ConnectionSnapshot,
        ticks: list[tuple[str, str, TickSnapshot | None]],
    ) -> None:
        self._connection = connection
        if not connection.connected:
            self._set_state("reconnecting", connection.reason)
            return
        self._set_state("connected")
        for canonical, broker, tick in ticks:
            quote = self._to_quote(canonical, broker, tick)
            if quote is None:
                continue
            self._check_offset_once(tick)
            previous = self._hub.last_quote(canonical)
            if previous is None or (previous.ts, previous.bid, previous.ask) != (
                quote.ts,
                quote.bid,
                quote.ask,
            ):
                self._hub.publish_quote(quote)

    def _to_quote(
        self, canonical: str, broker: str, tick: TickSnapshot | None
    ) -> MarketQuote | None:
        if tick is None or tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            return None
        if tick.time:
            ts = datetime.fromtimestamp(tick.time, UTC) - self._offset
        else:
            ts = self._clock()
        return MarketQuote(
            symbol=canonical,
            source_instrument=broker,
            provider=self.name,
            ts=ts,
            bid=tick.bid,
            ask=tick.ask,
        )

    def _check_offset_once(self, tick: TickSnapshot | None) -> None:
        """Warn, once, when the configured offset looks wrong.

        Only a warning: over a weekend or holiday the newest tick is genuinely
        old, and refusing to start then would be worse than a skewed log line.
        """
        if self._offset_checked or tick is None or not tick.time:
            return
        self._offset_checked = True
        corrected = datetime.fromtimestamp(tick.time, UTC) - self._offset
        drift = (self._clock() - corrected).total_seconds()
        if abs(drift) > OFFSET_TOLERANCE_SECONDS:
            log_event(
                "mt5_server_offset_suspect",
                level=logging.WARNING,
                configured_offset_seconds=int(self._offset.total_seconds()),
                newest_tick_drift_seconds=round(drift),
                hint="set MT5_SERVER_UTC_OFFSET_SECONDS to the broker-server UTC offset",
            )

    def _set_state(self, state: Any, error: str | None = None) -> None:
        """Publish a status event only on change, not on every poll."""
        if (self._hub.state, error) != (state, self._last_status_error):
            self._last_status_error = error
            self._hub.publish_status(state, error=error)

    # --- guards -------------------------------------------------------------

    def _call(self, function: Callable[..., Any], *args: Any) -> Any:
        with self._lock:
            return function(*args)

    @staticmethod
    def _require_feed(feed: str | None) -> None:
        if feed is not None:
            raise ServiceError(
                422, "feed_not_allowed", "An MT5 terminal has a single feed", {"feed": feed}
            )

    def _require_ready(self) -> None:
        if not self._initialized.is_set():
            raise ServiceError(503, "terminal_not_ready", "MetaTrader 5 is not initialized")
        connection = self._connection or self._call(self._adapter.connection_snapshot)
        if not connection.connected:
            raise ServiceError(
                503,
                "terminal_not_ready",
                "MetaTrader 5 terminal is not connected",
                {"reason": connection.reason},
            )

    def _broker_symbol(self, symbol: str) -> str:
        broker = self._manifest.get(symbol)
        if broker is None:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                "The symbol is not in this terminal's SYMBOLS_FILE",
                {"symbol": symbol, "configured": list(self._manifest)},
            )
        return broker

    def _ensure_selected(self, broker: str) -> None:
        info = self._call(self._adapter.symbol_info, broker)
        if info is None:
            raise ServiceError(422, "symbol_not_found", "The terminal does not know this symbol")
        if not info.visible and not self._call(self._adapter.symbol_select, broker):
            raise ServiceError(
                422, "symbol_unavailable", "The terminal could not select this symbol"
            )

    def _safe_last_error(self) -> Any:
        try:
            return self._call(self._adapter.last_error)
        except Exception:  # noqa: BLE001 - diagnostics only
            return None


def _instrument(canonical: str, info: SymbolSnapshot) -> InstrumentInfo:
    return InstrumentInfo(
        symbol=canonical,
        source_instrument=info.name,
        provider="mt5",
        digits=info.digits,
        price_increment=info.point,
        quantity_increment=info.volume_step,
        min_quantity=info.volume_min,
        max_quantity=info.volume_max,
    )
