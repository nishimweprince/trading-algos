"""APScheduler-based data fetching with caching"""
import threading
import time
from typing import Dict, List, Callable, Optional, Union
from datetime import datetime, timedelta
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

    def _fetch_instrument_data(self, instrument: str) -> Dict[str, pd.DataFrame]:
        """
        Fetch data for a single instrument across all timeframes.
        Returns dict: {timeframe: df}
        """
        results = {}
        logger.info(f"Fetching data for {instrument} ({len(self.timeframes)} timeframes: {self.timeframes})...")

        for timeframe in self.timeframes:
            try:
                logger.debug(f"Fetching {instrument} {timeframe}...")
                # Fetch candles (use reasonable count for real-time updates)
                count = 200  # Enough for strategy calculations
                df = self.feed.get_candles(instrument, timeframe, count=count)

                if not df.empty:
                    # Update cache
                    self.cache.update(instrument, timeframe, df)
                    results[timeframe] = df
                    logger.debug(f"Successfully fetched {len(df)} candles for {instrument} {timeframe}")
                else:
                    logger.warning(f"Empty data returned for {instrument} {timeframe} - API returned no candles")

            except Exception as e:
                logger.error(f"Error fetching {instrument} {timeframe}: {e}")
                import traceback
                logger.error(traceback.format_exc())
                # Continue with other timeframes
                continue

        logger.debug(
            f"Completed fetch for {instrument}: {len(results)}/{len(self.timeframes)} timeframes successful"
        )
        return results

    def _fetch_forex_data(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        Fetch data for all instrument/timeframe combinations.
        Returns dict: {instrument: {timeframe: df}}
        """
        results = {}
        total_requests = len(self.instruments) * len(self.timeframes)

        logger.info(f"Fetching data for {total_requests} combinations...")

        for instrument in self.instruments:
            results[instrument] = self._fetch_instrument_data(instrument)

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
        instruments_in_results = list(results.keys())
        logger.debug(
            f"Triggering {len(self.callbacks)} callback(s) with data for {len(instruments_in_results)} "
            f"instrument(s): {instruments_in_results}"
        )
        
        for callback in self.callbacks:
            try:
                callback(results)
            except Exception as e:
                logger.error(f"Error in data callback: {e}")
                import traceback
                logger.error(traceback.format_exc())

    def _scheduled_job_for_instrument(self, instrument: str) -> None:
        """
        Job executed by scheduler for a specific instrument.
        Fetches all timeframes for the instrument and triggers callbacks.
        """
        try:
            logger.debug(f"Starting scheduled fetch job for {instrument}")
            
            # Fetch data for this instrument
            timeframe_data = self._fetch_instrument_data(instrument)

            # Wrap in expected format: {instrument: {timeframe: df}}
            results = {instrument: timeframe_data}

            # Log what timeframes were successfully fetched
            fetched_timeframes = list(timeframe_data.keys())
            logger.debug(
                f"Fetched {len(fetched_timeframes)} timeframes for {instrument}: {fetched_timeframes}"
            )

            # Only trigger callbacks if we got some data
            if results[instrument]:
                logger.debug(f"Triggering callbacks for {instrument} with {len(fetched_timeframes)} timeframes")
                self._trigger_callbacks(results)
            else:
                logger.warning(f"No data fetched for {instrument} in this cycle - skipping callbacks")

        except Exception as e:
            logger.error(f"Error in scheduled data fetch for {instrument}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _scheduled_job(self) -> None:
        """Job executed by scheduler at regular intervals (legacy - fetches all instruments)"""
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
        """Start the scheduler with separate jobs for each instrument, spaced 1 minute apart"""
        if self._running:
            logger.warning("Scheduler already running")
            return

        # CRITICAL: Verify feed authenticated BEFORE starting
        if not self.feed.is_authenticated:
            logger.error("Cannot start scheduler: feed not authenticated")
            logger.error("Call feed.authenticate() before scheduler.start()")
            raise RuntimeError("Data feed must be authenticated before starting scheduler")

        # Start scheduler first
        self.scheduler.start()
        self._running = True

        # Create separate scheduled job for each instrument with 1-minute offsets
        now = datetime.now()
        for index, instrument in enumerate(self.instruments):
            # Calculate offset: 1 minute (60 seconds) per instrument
            offset_seconds = index * 60
            start_time = now + timedelta(seconds=offset_seconds)

            # Create job ID unique to instrument
            job_id = f'forex_data_fetch_{instrument}'
            job_name = f'Forex Data Fetch - {instrument}'

            # Add job with staggered start time
            self.scheduler.add_job(
                func=self._scheduled_job_for_instrument,
                args=[instrument],
                trigger=IntervalTrigger(seconds=self.fetch_interval),
                id=job_id,
                name=job_name,
                replace_existing=True,
                next_run_time=start_time
            )

            logger.info(
                f"Scheduled {instrument}: fetching every {self.fetch_interval}s, "
                f"first fetch in {offset_seconds}s"
            )

        # Perform staggered initial fetches to avoid rate limits on startup
        logger.info("Scheduler started. Performing staggered initial data fetches...")
        for index, instrument in enumerate(self.instruments):
            try:
                # Wait 1 minute before each instrument (except first)
                if index > 0:
                    logger.info(f"Waiting 60s before initial fetch for {instrument}...")
                    time.sleep(60)

                # Perform initial fetch
                logger.info(f"Performing initial fetch for {instrument}...")
                self._scheduled_job_for_instrument(instrument)
                logger.info(f"Initial fetch completed for {instrument}")
            except Exception as e:
                logger.error(f"Initial fetch failed for {instrument}: {e}")
                logger.error(f"Will retry at next scheduled interval for {instrument}")

        logger.info(
            f"Scheduler running: {len(self.instruments)} instruments, "
            f"fetching every {self.fetch_interval} seconds, spaced 1 minute apart"
        )

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
