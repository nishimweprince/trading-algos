"""APScheduler-based data fetching with caching"""
import threading
import time
from typing import Dict, List, Callable, Optional, Union
from datetime import datetime
import pandas as pd
from loguru import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .capital_feed import CapitalDataFeed


class DataCache:
    """Thread-safe in-memory cache for market data"""

    def __init__(self):
        self._cache: Dict[str, Dict[str, pd.DataFrame]] = {}  # {instrument: {timeframe: df}}
        self._last_update: Dict[str, Dict[str, datetime]] = {}  # {instrument: {timeframe: timestamp}}
        self._lock = threading.Lock()

    def update(self, instrument: str, timeframe: str, data: pd.DataFrame) -> None:
        """Store DataFrame in cache"""
        with self._lock:
            if instrument not in self._cache:
                self._cache[instrument] = {}
                self._last_update[instrument] = {}

            self._cache[instrument][timeframe] = data.copy()
            self._last_update[instrument][timeframe] = datetime.now()
            logger.debug(f"Cache updated: {instrument} {timeframe} ({len(data)} candles)")

    def get(self, instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Retrieve cached DataFrame"""
        with self._lock:
            if instrument in self._cache and timeframe in self._cache[instrument]:
                return self._cache[instrument][timeframe].copy()
            return None

    def get_last_update(self, instrument: str, timeframe: str) -> Optional[datetime]:
        """Get timestamp of last cache update"""
        with self._lock:
            if instrument in self._last_update and timeframe in self._last_update[instrument]:
                return self._last_update[instrument][timeframe]
            return None

    def clear(self, instrument: Optional[str] = None) -> None:
        """Clear cache for specific instrument or all instruments"""
        with self._lock:
            if instrument:
                if instrument in self._cache:
                    del self._cache[instrument]
                if instrument in self._last_update:
                    del self._last_update[instrument]
                logger.debug(f"Cache cleared for {instrument}")
            else:
                self._cache.clear()
                self._last_update.clear()
                logger.debug("Cache cleared for all instruments")


class ForexDataScheduler:
    """APScheduler-based periodic data fetcher for forex instruments"""

    def __init__(self, feed: CapitalDataFeed, instruments: List[str],
                 timeframes: List[str], fetch_interval_seconds: int = 60):
        """
        Args:
            feed: CapitalDataFeed instance
            instruments: List of instruments to fetch (e.g., ['EUR_USD', 'GBP_USD'])
            timeframes: List of timeframes to fetch (e.g., ['1H', '4H'])
            fetch_interval_seconds: How often to fetch data (default 60 seconds)
        """
        self.feed = feed
        self.instruments = instruments
        self.timeframes = timeframes
        self.fetch_interval = fetch_interval_seconds
        self.cache = DataCache()
        self.scheduler = BackgroundScheduler()
        self.callbacks: List[Callable] = []
        self._running = False

        logger.info(f"ForexDataScheduler initialized: {len(instruments)} instruments, "
                   f"{len(timeframes)} timeframes, {fetch_interval_seconds}s interval")

    def _fetch_forex_data(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        Fetch data for all instrument/timeframe combinations.
        Returns dict: {instrument: {timeframe: df}}
        """
        results = {}
        total_requests = len(self.instruments) * len(self.timeframes)

        logger.info(f"Fetching data for {total_requests} combinations...")

        for instrument in self.instruments:
            results[instrument] = {}

            for timeframe in self.timeframes:
                try:
                    # Fetch candles (use reasonable count for real-time updates)
                    count = 200  # Enough for strategy calculations
                    df = self.feed.get_candles(instrument, timeframe, count=count)

                    if not df.empty:
                        # Update cache
                        self.cache.update(instrument, timeframe, df)
                        results[instrument][timeframe] = df
                        logger.debug(f"Fetched {len(df)} candles: {instrument} {timeframe}")
                    else:
                        logger.warning(f"Empty data returned for {instrument} {timeframe}")

                except Exception as e:
                    logger.error(f"Error fetching {instrument} {timeframe}: {e}")
                    # Continue with other combinations
                    continue

        return results

    def on_data_fetched(self, callback: Callable[[Dict[str, Dict[str, pd.DataFrame]]], None]) -> None:
        """
        Register callback function to be called when new data is fetched.

        Args:
            callback: Function that takes results dict: {instrument: {timeframe: df}}
        """
        self.callbacks.append(callback)
        logger.info(f"Registered data callback (total: {len(self.callbacks)})")

    def _trigger_callbacks(self, results: Dict[str, Dict[str, pd.DataFrame]]) -> None:
        """Call all registered callbacks with fetched data"""
        for callback in self.callbacks:
            try:
                callback(results)
            except Exception as e:
                logger.error(f"Error in data callback: {e}")

    def _scheduled_job(self) -> None:
        """Job executed by scheduler at regular intervals"""
        try:
            results = self._fetch_forex_data()

            # Only trigger callbacks if we got some data
            if results:
                self._trigger_callbacks(results)
            else:
                logger.warning("No data fetched in this cycle")

        except Exception as e:
            logger.error(f"Error in scheduled data fetch: {e}")

    def start(self) -> None:
        """Start the scheduler"""
        if self._running:
            logger.warning("Scheduler is already running")
            return

        # Add job to scheduler
        self.scheduler.add_job(
            func=self._scheduled_job,
            trigger=IntervalTrigger(seconds=self.fetch_interval),
            id='forex_data_fetch',
            name='Forex Data Fetch',
            replace_existing=True
        )

        # Start scheduler
        self.scheduler.start()
        self._running = True

        # Trigger initial fetch
        logger.info("Scheduler started. Performing initial data fetch...")
        self._scheduled_job()

        logger.info(f"Scheduler running: fetching every {self.fetch_interval} seconds")

    def stop(self) -> None:
        """Stop the scheduler"""
        if not self._running:
            logger.warning("Scheduler is not running")
            return

        self.scheduler.shutdown(wait=True)
        self._running = False
        logger.info("Scheduler stopped")

    def is_running(self) -> bool:
        """Check if scheduler is running"""
        return self._running

    def get_cached_data(self, instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Get cached data for instrument/timeframe"""
        return self.cache.get(instrument, timeframe)

    def get_all_cached_data(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """Get all cached data"""
        result = {}
        with self.cache._lock:
            for instrument in self.cache._cache:
                result[instrument] = {}
                for timeframe in self.cache._cache[instrument]:
                    result[instrument][timeframe] = self.cache._cache[instrument][timeframe].copy()
        return result
