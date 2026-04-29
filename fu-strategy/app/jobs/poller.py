"""Background market poller.

Every `polling_interval_seconds`, fetches a small tail of OHLC candles from
Capital.com for each configured (symbol, timeframe), feeds newly-closed
candles into the SignalGenerator pipeline, and broadcasts any resulting
Signal to all NOTIFICATION_NUMBERS via the NotificationDispatcher.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Optional, Tuple

import pandas as pd
from loguru import logger

from app.config import Settings
from app.data.feed import CapitalDataFeed
from app.engine.signal_generator import SignalGenerator
from app.notifications.dispatcher import NotificationDispatcher


class MarketPoller:
    def __init__(
        self,
        settings: Settings,
        signal_generator: SignalGenerator,
        dispatcher: NotificationDispatcher,
        feed: CapitalDataFeed,
    ):
        self.settings = settings
        self.signal_generator = signal_generator
        self.dispatcher = dispatcher
        self.feed = feed
        self.interval_s = max(1, int(settings.polling_interval_seconds))
        self.tail = max(2, int(settings.polling_tail_candles))
        self._last_seen: Dict[Tuple[str, str], pd.Timestamp] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._seeded = False

    @property
    def timeframes(self):
        return list(dict.fromkeys([
            *self.settings.htf_timeframes,
            *self.settings.ltf_timeframes,
        ]))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name='market-poller')
        logger.info(
            f"MarketPoller started (interval={self.interval_s}s, "
            f"symbols={self.settings.symbols}, timeframes={self.timeframes})"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("MarketPoller stopped")

    async def _run(self) -> None:
        try:
            await self._seed_history()
        except Exception:
            logger.exception("MarketPoller seeding failed; continuing without seed")

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("MarketPoller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    async def _seed_history(self) -> None:
        if self._seeded:
            return
        if not await asyncio.to_thread(self.feed.authenticate):
            logger.error("MarketPoller: Capital.com authentication failed; not seeded")
            return

        for symbol in self.settings.symbols:
            for tf in self.timeframes:
                try:
                    df = await asyncio.to_thread(
                        self.feed.get_candles, symbol, tf, self.settings.backfill_candles,
                    )
                    if df is None or df.empty:
                        logger.warning(f"Seed: no candles for {symbol} {tf}")
                        continue
                    self.signal_generator.seed(symbol, tf, df, log_events=False)
                    self._last_seen[(symbol, tf)] = df.index.max()
                    logger.info(f"Seeded history for {symbol} {tf} ({len(df)} candles)")
                except Exception:
                    logger.exception(f"Seed failed for {symbol} {tf}")
        self._seeded = True

    async def _tick(self) -> None:
        if not self._seeded:
            await self._seed_history()
            if not self._seeded:
                return

        for symbol in self.settings.symbols:
            for tf in self.timeframes:
                try:
                    await self._poll_one(symbol, tf)
                except Exception:
                    logger.exception(f"Poll failed for {symbol} {tf}")

    async def _poll_one(self, symbol: str, tf: str) -> None:
        df = await asyncio.to_thread(self.feed.get_candles, symbol, tf, self.tail)
        if df is None or df.empty or len(df) < 2:
            return

        # Last bar is still forming; closed bars are everything before it.
        closed = df.iloc[:-1]
        forming_ts = df.index[-1]
        forming_row = df.iloc[-1]
        last_seen = self._last_seen.get((symbol, tf))
        if last_seen is not None:
            closed = closed[closed.index > last_seen]

        if not closed.empty:
            logger.debug(f"Polled {symbol} {tf}: {len(closed)} new candle(s)")
            for ts, row in closed.iterrows():
                payload = self._row_payload(ts, row)
                try:
                    signal = self.signal_generator.on_new_candle(symbol, tf, payload)
                except Exception:
                    logger.exception(f"on_new_candle raised for {symbol} {tf} @ {ts}")
                    signal = None

                self._last_seen[(symbol, tf)] = ts

                if signal is not None:
                    await self._emit_signal(signal)

        if (self.settings.fu_fire_on_forming
                and tf in self.settings.ltf_timeframes):
            payload = self._row_payload(forming_ts, forming_row)
            try:
                signal = self.signal_generator.on_new_candle(
                    symbol, tf, payload, forming=True,
                )
            except Exception:
                logger.exception(
                    f"on_new_candle (forming) raised for {symbol} {tf} @ {forming_ts}"
                )
                signal = None
            if signal is not None:
                await self._emit_signal(signal)

    @staticmethod
    def _row_payload(ts, row) -> dict:
        return {
            'timestamp': ts,
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row.get('volume', 0.0) or 0.0),
        }

    async def _emit_signal(self, signal) -> None:
        logger.info(
            f"Signal generated: {signal.id} {signal.symbol} {signal.timeframe} "
            f"{signal.direction.value} entry={signal.entry_price} sl={signal.sl} "
            f"tp={signal.tp} rr={signal.rr}"
        )
        try:
            await self.dispatcher.notify_signal(signal)
        except Exception:
            logger.exception(f"notify_signal failed for {signal.id}")
