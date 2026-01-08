"""Strategy Runner - manages multiple trading pairs"""
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List, Callable
import pandas as pd
from loguru import logger

# Add project root to path for imports
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import load_config, StrategyConfig
from data import CapitalDataFeed, ForexDataScheduler
from strategy import SignalGenerator, Signal, SignalType
from notifications import EmailNotifier


@dataclass
class PairRunner:
    """Manages signal generation for a single trading pair"""
    instrument: str
    signal_generator: SignalGenerator
    last_signal: Optional[Signal] = None
    last_update: Optional[datetime] = None
    status: str = "stopped"
    error_message: Optional[str] = None
    candles_ltf: int = 0
    candles_htf: int = 0


class StrategyRunner:
    """
    Manages multiple trading pairs running in the background.
    Uses a single scheduler and data feed for efficiency.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or load_config()
        self.feed: Optional[CapitalDataFeed] = None
        self.scheduler: Optional[ForexDataScheduler] = None
        self.pairs: Dict[str, PairRunner] = {}
        self.running = False
        self.authenticated = False
        self.started_at: Optional[datetime] = None
        self._lock = threading.Lock()
        self._signal_callbacks: List[Callable[[str, Signal], None]] = []

        # Initialize email notifier
        self.email_notifier = EmailNotifier()
        if self.email_notifier.is_enabled:
            logger.info("Email notifications enabled")
        else:
            logger.info("Email notifications disabled (RESEND_API_KEY or NOTIFICATION_EMAILS not configured)")

        # Initialize pair runners from config
        for instrument in self.config.trading.instruments:
            self._add_pair(instrument)

        logger.info(f"StrategyRunner initialized with {len(self.pairs)} pairs: {list(self.pairs.keys())}")

    def _add_pair(self, instrument: str) -> None:
        """Add a new pair runner"""
        if instrument not in self.pairs:
            self.pairs[instrument] = PairRunner(
                instrument=instrument,
                signal_generator=SignalGenerator(self.config),
                status="stopped"
            )
            logger.info(f"Added pair runner for {instrument}")

    def add_signal_callback(self, callback: Callable[[str, Signal], None]) -> None:
        """Register a callback for new signals"""
        self._signal_callbacks.append(callback)

    def _notify_signal(self, instrument: str, signal: Signal) -> None:
        """Notify all registered callbacks of a new signal"""
        for callback in self._signal_callbacks:
            try:
                callback(instrument, signal)
            except Exception as e:
                logger.error(f"Error in signal callback: {e}")

    def _send_email_notification(self, instrument: str, signal: Signal) -> None:
        """Send email notification for a trading signal"""
        if not self.email_notifier.is_enabled:
            return

        try:
            signal_type_str = signal.type.name if isinstance(signal.type, SignalType) else str(signal.type)
            
            self.email_notifier.send_signal_notification(
                instrument=instrument,
                signal_type=signal_type_str,
                price=signal.price,
                strength=signal.strength,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                reasons=signal.reasons
            )
            logger.debug(f"Email notification queued for {instrument} {signal_type_str} signal")
        except Exception as e:
            logger.error(f"Error sending email notification: {e}")

    def start(self) -> bool:
        """Start all pair runners"""
        if self.running:
            logger.warning("Strategy runner already running")
            return True

        logger.info("Starting strategy runner...")

        # Validate Capital.com configuration
        capitalcom_issues = self.config.capitalcom.validate()
        if capitalcom_issues:
            for issue in capitalcom_issues:
                logger.error(f"Config issue: {issue}")
            return False

        try:
            # Initialize data feed
            self.feed = CapitalDataFeed(
                api_key=self.config.capitalcom.api_key,
                password=self.config.capitalcom.api_password,
                username=self.config.capitalcom.username,
                environment=self.config.capitalcom.environment
            )

            # Authenticate
            logger.info("Authenticating with Capital.com...")
            if not self.feed.authenticate():
                logger.error("Authentication failed")
                self.authenticated = False
                return False

            self.authenticated = True
            logger.info("Authentication successful")

            # Create scheduler for all pairs
            instruments = list(self.pairs.keys())
            timeframes = [self.config.trading.timeframe, self.config.trading.htf_timeframe]
            fetch_interval_seconds = self.config.trading.fetch_interval_minutes * 60

            self.scheduler = ForexDataScheduler(
                feed=self.feed,
                instruments=instruments,
                timeframes=timeframes,
                fetch_interval_seconds=fetch_interval_seconds
            )

            # Register data callback
            self.scheduler.on_data_fetched(self._on_data_received)

            # Start scheduler
            self.scheduler.start()

            # Update all pairs to running
            with self._lock:
                for pair in self.pairs.values():
                    pair.status = "running"

            self.running = True
            self.started_at = datetime.now()

            logger.info(f"Strategy runner started with {len(instruments)} pairs")
            logger.info(f"Fetch interval: {self.config.trading.fetch_interval_minutes} minutes")
            logger.info(f"Timeframes: LTF={self.config.trading.timeframe}, HTF={self.config.trading.htf_timeframe}")

            return True

        except Exception as e:
            logger.error(f"Failed to start strategy runner: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def stop(self) -> None:
        """Stop all pair runners"""
        if not self.running:
            logger.warning("Strategy runner not running")
            return

        logger.info("Stopping strategy runner...")

        try:
            if self.scheduler:
                self.scheduler.stop()

            if self.feed:
                self.feed.logout()

            with self._lock:
                for pair in self.pairs.values():
                    pair.status = "stopped"

            self.running = False
            self.authenticated = False
            logger.info("Strategy runner stopped")

        except Exception as e:
            logger.error(f"Error stopping strategy runner: {e}")

    def _on_data_received(self, results: Dict[str, Dict[str, pd.DataFrame]]) -> None:
        """Callback when new data is fetched"""
        ltf = self.config.trading.timeframe
        htf = self.config.trading.htf_timeframe

        for instrument, pair in self.pairs.items():
            try:
                # Get cached data for both timeframes
                ltf_df = self.scheduler.get_cached_data(instrument, ltf)
                htf_df = self.scheduler.get_cached_data(instrument, htf)

                if ltf_df is None or htf_df is None:
                    logger.warning(f"Incomplete data for {instrument}")
                    pair.error_message = "Incomplete data"
                    continue

                if len(ltf_df) == 0 or len(htf_df) == 0:
                    logger.warning(f"Empty dataframes for {instrument}")
                    pair.error_message = "Empty dataframes"
                    continue

                # Update candle counts
                with self._lock:
                    pair.candles_ltf = len(ltf_df)
                    pair.candles_htf = len(htf_df)
                    pair.error_message = None

                # Generate signal
                signal = pair.signal_generator.get_current_signal(
                    ltf_df,
                    htf_df,
                    instrument,
                    current_position=0
                )

                # Update pair state
                with self._lock:
                    pair.last_signal = signal
                    pair.last_update = datetime.now()

                # Log signal
                signal_type_str = signal.type.name if isinstance(signal.type, SignalType) else str(signal.type)
                if signal.type != SignalType.NONE:
                    logger.info(
                        f"SIGNAL [{instrument}]: {signal_type_str} @ {signal.price:.5f} "
                        f"(strength: {signal.strength:.2f}) - {', '.join(signal.reasons)}"
                    )
                    self._notify_signal(instrument, signal)
                    
                    # Send email notification
                    self._send_email_notification(instrument, signal)
                else:
                    logger.debug(f"[{instrument}] No signal at {signal.price:.5f}")

            except Exception as e:
                logger.error(f"Error processing {instrument}: {e}")
                import traceback
                logger.error(traceback.format_exc())
                with self._lock:
                    pair.status = "error"
                    pair.error_message = str(e)

    def get_pair_status(self, instrument: str) -> Optional[PairRunner]:
        """Get status for a specific pair"""
        return self.pairs.get(instrument)

    def get_all_pairs_status(self) -> Dict[str, PairRunner]:
        """Get status for all pairs"""
        return self.pairs.copy()

    def get_latest_signal(self, instrument: str) -> Optional[Signal]:
        """Get the latest signal for a specific pair"""
        pair = self.pairs.get(instrument)
        if pair:
            return pair.last_signal
        return None

    def is_healthy(self) -> bool:
        """Check if the runner is healthy"""
        if not self.running:
            return False
        if not self.authenticated:
            return False
        if self.scheduler and not self.scheduler.is_running():
            return False
        return True

    def get_running_pairs_count(self) -> int:
        """Get count of running pairs"""
        return sum(1 for p in self.pairs.values() if p.status == "running")
