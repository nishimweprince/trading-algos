#!/usr/bin/env python3
"""
Live Trading Example

Demonstrates how to set up live trading with OANDA.
Note: Requires OANDA credentials in .env file.
"""

import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta

from config.settings import get_settings
from data.feed import OANDADataFeed
from data.resampler import TimeframeResampler
from execution.broker import OANDABroker
from execution.position_manager import PositionManager, PositionSide
from indicators import SupertrendIndicator, StochRSIIndicator
from monitoring.alerts import AlertManager
from monitoring.logger import setup_logging, get_logger
from risk.exposure import ExposureManager
from risk.position_sizing import PositionSizer
from risk.stop_loss import StopLossCalculator
from strategy.filters import TradingFilters
from strategy.signal_generator import SignalGenerator, SignalType

setup_logging(log_level="INFO")
logger = get_logger(__name__)


class SimpleLiveTrader:
    """
    Simple live trading implementation.
    
    This is a demonstration of how to structure live trading.
    In production, you would run this in a proper scheduler or daemon.
    """
    
    def __init__(self, instrument: str = "EUR_USD"):
        """Initialize the trader."""
        self.instrument = instrument
        self.settings = get_settings()
        
        # Components
        self.data_feed = OANDADataFeed()
        self.broker = OANDABroker()
        self.signal_generator = SignalGenerator()
        self.position_manager = PositionManager()
        self.position_sizer = PositionSizer(account_balance=10000.0)
        self.stop_calculator = StopLossCalculator()
        self.exposure_manager = ExposureManager(account_balance=10000.0)
        self.filters = TradingFilters.create_default()
        self.alerts = AlertManager()
        
        logger.info(f"Initialized trader for {instrument}")
    
    def check_connection(self) -> bool:
        """Check broker connection."""
        summary = self.broker.get_account_summary()
        if summary:
            logger.info(f"Connected to OANDA. Balance: ${summary['balance']:,.2f}")
            self.position_sizer.update_balance(summary["balance"])
            self.exposure_manager.update_balance(summary["balance"])
            return True
        else:
            logger.warning("Not connected to OANDA (using paper mode)")
            return False
    
    def sync_positions(self):
        """Sync positions from broker."""
        open_trades = self.broker.get_open_trades(self.instrument)
        
        for trade in open_trades:
            # Check if we're already tracking this position
            existing = [p for p in self.position_manager.open_positions 
                       if p.id == trade["trade_id"]]
            
            if not existing:
                # Add to tracking
                is_long = trade["units"] > 0
                self.position_manager.create_position(
                    instrument=self.instrument,
                    side=PositionSide.LONG if is_long else PositionSide.SHORT,
                    units=abs(trade["units"]),
                    entry_price=trade["price"],
                    stop_loss=trade["stop_loss"],
                    take_profit=trade["take_profit"],
                )
                logger.info(f"Synced existing position: {trade['trade_id']}")
    
    def fetch_data(self, lookback_hours: int = 200):
        """Fetch recent data from OANDA."""
        logger.info(f"Fetching last {lookback_hours} hours of data...")
        
        df_1h = self.data_feed.get_candles(
            instrument=self.instrument,
            granularity="H1",
            count=lookback_hours,
        )
        
        if df_1h.empty:
            logger.error("Failed to fetch data")
            return None, None
        
        # Resample to 4H
        resampler = TimeframeResampler(df_1h)
        df_4h = resampler.resample("H4", shift=True)
        
        logger.info(f"Fetched {len(df_1h)} 1H bars, {len(df_4h)} 4H bars")
        return df_1h, df_4h
    
    def check_for_entry(self, df_1h, df_4h) -> dict:
        """Check for entry signals."""
        # Generate signal
        self.signal_generator.prepare_data(df_1h, df_4h)
        signal = self.signal_generator.get_current_signal()
        
        if signal is None:
            return None
        
        # Validate with filters
        is_valid, reasons = self.filters.validate_signal(signal.timestamp)
        if not is_valid:
            logger.debug(f"Signal filtered: {reasons}")
            return None
        
        # Check exposure
        can_open, reason = self.exposure_manager.can_open_position(
            instrument=self.instrument,
            risk_amount=self.position_sizer.account_balance * 0.02,
            position_manager=self.position_manager,
        )
        
        if not can_open:
            logger.info(f"Cannot open: {reason}")
            return None
        
        return {
            "signal": signal,
            "indicators": self.signal_generator.get_indicator_values(),
        }
    
    def execute_entry(self, df_1h, signal_data):
        """Execute an entry signal."""
        signal = signal_data["signal"]
        is_long = signal.type == SignalType.BUY
        
        # Calculate stops
        stops = self.stop_calculator.calculate_atr_stop(
            df=df_1h,
            entry_price=signal.price,
            is_long=is_long,
        )
        
        # Calculate position size
        pos_size = self.position_sizer.calculate_fixed_risk(
            entry_price=signal.price,
            stop_loss=stops.stop_loss,
        )
        
        units = pos_size.units if is_long else -pos_size.units
        
        logger.info(
            f"Executing {signal.type.name} {self.instrument}: "
            f"{abs(units)} units @ ~{signal.price:.5f}, "
            f"SL: {stops.stop_loss:.5f}, TP: {stops.take_profit:.5f}"
        )
        
        # Send alert
        self.alerts.signal_alert(
            instrument=self.instrument,
            signal_type="BUY" if is_long else "SELL",
            price=signal.price,
            strength=signal.strength,
        )
        
        # Place order
        result = self.broker.place_market_order(
            instrument=self.instrument,
            units=units,
            stop_loss=stops.stop_loss,
            take_profit=stops.take_profit,
        )
        
        if result.success:
            logger.info(f"Order filled: {result.trade_id}")
            
            # Track position
            self.position_manager.create_position(
                instrument=self.instrument,
                side=PositionSide.LONG if is_long else PositionSide.SHORT,
                units=abs(units),
                entry_price=result.fill_price or signal.price,
                stop_loss=stops.stop_loss,
                take_profit=stops.take_profit,
                signal_strength=signal.strength,
            )
            
            self.alerts.trade_alert(
                instrument=self.instrument,
                action="OPENED",
                side="BUY" if is_long else "SELL",
                units=abs(units),
                price=result.fill_price or signal.price,
            )
        else:
            logger.error(f"Order failed: {result.error_message}")
    
    def run_once(self):
        """Run a single trading check."""
        logger.info(f"\n{'='*50}")
        logger.info(f"Running check at {datetime.now()}")
        logger.info(f"{'='*50}")
        
        # Check connection and sync
        self.check_connection()
        self.sync_positions()
        
        # Fetch data
        df_1h, df_4h = self.fetch_data()
        if df_1h is None:
            return
        
        # Check for entry
        if len(self.position_manager.open_positions) == 0:
            signal_data = self.check_for_entry(df_1h, df_4h)
            
            if signal_data:
                self.execute_entry(df_1h, signal_data)
            else:
                logger.info("No signal at this time")
        else:
            logger.info(f"Already have {len(self.position_manager.open_positions)} open position(s)")
        
        # Print status
        stats = self.position_manager.get_statistics()
        logger.info(
            f"Status: {stats['total_trades']} trades, "
            f"{stats['win_rate']*100:.1f}% win rate, "
            f"${stats['total_pnl']:.2f} P&L"
        )


def main():
    """Main entry point for live trading example."""
    print("\n" + "=" * 60)
    print("LIVE TRADING EXAMPLE")
    print("=" * 60)
    print("\nThis example demonstrates live trading with OANDA.")
    print("Make sure you have configured your .env file with:")
    print("  - OANDA_ACCESS_TOKEN")
    print("  - OANDA_ACCOUNT_ID")
    print("  - OANDA_ENVIRONMENT (practice or live)")
    print("\nStarting in paper mode if not configured...\n")
    
    trader = SimpleLiveTrader(instrument="EUR_USD")
    
    # Run once
    trader.run_once()
    
    print("\n" + "=" * 60)
    print("For continuous trading, you would typically:")
    print("1. Run this in a scheduler (cron, APScheduler)")
    print("2. Execute every hour when new candle closes")
    print("3. Monitor logs and alerts for issues")
    print("=" * 60)


if __name__ == "__main__":
    main()
