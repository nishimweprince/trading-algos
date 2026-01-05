#!/usr/bin/env python3
"""
Forex MTF Strategy - Main Orchestrator

Multi-timeframe forex trading strategy combining:
- 4H Supertrend for trend filtering
- 1H Stochastic RSI for momentum
- Fair Value Gap price action analysis
- Volume Profile zone identification

Usage:
    # Backtest mode
    python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01
    
    # Paper trading mode
    python main.py paper --instrument EUR_USD
    
    # Live trading mode (requires OANDA credentials)
    python main.py live --instrument EUR_USD
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import Settings, get_settings
from data.feed import OANDADataFeed
from data.historical import HistoricalDataLoader
from data.resampler import TimeframeResampler
from execution.broker import OANDABroker
from execution.position_manager import Position, PositionManager, PositionSide
from indicators import FVGDetector, StochRSIIndicator, SupertrendIndicator, VolumeProfileCalculator
from monitoring.alerts import AlertManager
from monitoring.logger import get_logger, setup_logging, TradeLogger
from risk.exposure import ExposureManager
from risk.position_sizing import PositionSizer
from risk.stop_loss import StopLossCalculator
from strategy.filters import TradingFilters
from strategy.signal_generator import SignalGenerator, SignalType

logger = get_logger(__name__)


class TradingBot:
    """
    Main trading bot orchestrator.
    
    Coordinates all components for live/paper trading.
    """
    
    def __init__(
        self,
        instrument: str = "EUR_USD",
        settings: Optional[Settings] = None,
    ):
        """
        Initialize trading bot.
        
        Args:
            instrument: Trading instrument
            settings: Settings instance (uses global if not provided)
        """
        self.instrument = instrument
        self.settings = settings or get_settings()
        
        # Initialize components
        self.data_feed = OANDADataFeed()
        self.broker = OANDABroker()
        self.signal_generator = SignalGenerator()
        self.position_manager = PositionManager()
        self.position_sizer = PositionSizer()
        self.stop_calculator = StopLossCalculator()
        self.exposure_manager = ExposureManager()
        self.filters = TradingFilters.create_default()
        self.alerts = AlertManager()
        self.trade_logger = TradeLogger()
        
        # State
        self._running = False
        self._df_1h: Optional[pd.DataFrame] = None
        self._df_4h: Optional[pd.DataFrame] = None
    
    def load_data(self, lookback_days: int = 30):
        """
        Load historical data for indicator calculation.
        
        Args:
            lookback_days: Number of days of history to load
        """
        logger.info(f"Loading {lookback_days} days of data for {self.instrument}")
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=lookback_days)
        
        # Load 1H data
        self._df_1h = self.data_feed.get_candles_range(
            instrument=self.instrument,
            granularity="H1",
            from_time=start_time,
            to_time=end_time,
        )
        
        if self._df_1h.empty:
            logger.warning("No 1H data available from feed, generating sample data")
            loader = HistoricalDataLoader()
            self._df_1h = loader.generate_sample_data(
                instrument=self.instrument,
                start=start_time,
                end=end_time,
                granularity="H1",
            )
        
        # Resample to 4H
        resampler = TimeframeResampler(self._df_1h)
        self._df_4h = resampler.resample("H4", shift=True)
        
        logger.info(f"Loaded {len(self._df_1h)} 1H bars, {len(self._df_4h)} 4H bars")
    
    def check_for_signals(self) -> Optional[dict]:
        """
        Check for new trading signals.
        
        Returns:
            Signal dict or None
        """
        if self._df_1h is None or len(self._df_1h) < 100:
            logger.warning("Insufficient data for signal generation")
            return None
        
        # Generate signal
        self.signal_generator.prepare_data(self._df_1h, self._df_4h)
        signal = self.signal_generator.get_current_signal()
        
        if signal is None:
            return None
        
        # Validate with filters
        is_valid, reasons = self.filters.validate_signal(
            timestamp=signal.timestamp,
        )
        
        if not is_valid:
            logger.debug(f"Signal rejected: {reasons}")
            return None
        
        # Check exposure
        can_open, reason = self.exposure_manager.can_open_position(
            instrument=self.instrument,
            risk_amount=self.settings.risk.max_risk_per_trade * self.position_sizer.account_balance,
            position_manager=self.position_manager,
        )
        
        if not can_open:
            logger.info(f"Cannot open position: {reason}")
            return None
        
        return {
            "signal": signal,
            "indicators": self.signal_generator.get_indicator_values(),
        }
    
    def execute_signal(self, signal_data: dict):
        """
        Execute a trading signal.
        
        Args:
            signal_data: Signal data dict from check_for_signals
        """
        signal = signal_data["signal"]
        indicators = signal_data["indicators"]
        
        is_long = signal.type == SignalType.BUY
        
        # Calculate stop levels
        stop_levels = self.stop_calculator.calculate_atr_stop(
            df=self._df_1h,
            entry_price=signal.price,
            is_long=is_long,
        )
        
        # Calculate position size
        position_size = self.position_sizer.calculate_fixed_risk(
            entry_price=signal.price,
            stop_loss=stop_levels.stop_loss,
        )
        
        units = position_size.units if is_long else -position_size.units
        
        # Log signal
        self.trade_logger.log_signal(
            instrument=self.instrument,
            signal_type="BUY" if is_long else "SELL",
            price=signal.price,
            strength=signal.strength,
            indicators=indicators,
        )
        
        # Send alert
        self.alerts.signal_alert(
            instrument=self.instrument,
            signal_type="BUY" if is_long else "SELL",
            price=signal.price,
            strength=signal.strength,
            details=f"StochRSI: {signal.stochrsi_value:.1f}\nTrend: {'Bullish' if signal.trend_direction == 1 else 'Bearish'}",
        )
        
        # Place order
        result = self.broker.place_market_order(
            instrument=self.instrument,
            units=units,
            stop_loss=stop_levels.stop_loss,
            take_profit=stop_levels.take_profit,
        )
        
        if result.success:
            # Track position
            position = self.position_manager.create_position(
                instrument=self.instrument,
                side=PositionSide.LONG if is_long else PositionSide.SHORT,
                units=abs(units),
                entry_price=result.fill_price or signal.price,
                stop_loss=stop_levels.stop_loss,
                take_profit=stop_levels.take_profit,
                signal_strength=signal.strength,
            )
            
            self.trade_logger.log_fill(
                instrument=self.instrument,
                trade_id=position.id,
                side="BUY" if is_long else "SELL",
                units=abs(units),
                fill_price=result.fill_price or signal.price,
            )
            
            self.alerts.trade_alert(
                instrument=self.instrument,
                action="OPENED",
                side="BUY" if is_long else "SELL",
                units=abs(units),
                price=result.fill_price or signal.price,
            )
            
            logger.info(f"Position opened: {position.id}")
        else:
            logger.error(f"Order failed: {result.error_message}")
    
    def manage_positions(self):
        """Manage open positions (check stops, trailing, etc.)."""
        if self._df_1h is None or self._df_1h.empty:
            return
        
        current_candle = self._df_1h.iloc[-1]
        current_time = self._df_1h.index[-1]
        
        # Check stops
        closed_positions = self.position_manager.check_stops(
            instrument=self.instrument,
            current_high=current_candle["high"],
            current_low=current_candle["low"],
            current_time=current_time,
        )
        
        for position in closed_positions:
            self.trade_logger.log_close(
                instrument=self.instrument,
                trade_id=position.id,
                entry_price=position.entry_price,
                exit_price=position.exit_price,
                pnl=position.realized_pnl,
                duration_hours=position.duration.total_seconds() / 3600 if position.duration else 0,
            )
            
            self.alerts.trade_alert(
                instrument=self.instrument,
                action="CLOSED",
                side="LONG" if position.is_long else "SHORT",
                units=position.units,
                price=position.exit_price,
                pnl=position.realized_pnl,
            )
    
    def run_iteration(self):
        """Run a single trading iteration."""
        # Refresh data
        self.load_data(lookback_days=7)
        
        # Manage existing positions
        self.manage_positions()
        
        # Check for new signals
        signal_data = self.check_for_signals()
        
        if signal_data:
            self.execute_signal(signal_data)
    
    def get_status(self) -> dict:
        """Get current bot status."""
        stats = self.position_manager.get_statistics()
        exposure = self.exposure_manager.get_exposure_status(self.position_manager)
        
        return {
            "instrument": self.instrument,
            "open_positions": len(self.position_manager.open_positions),
            "total_trades": stats["total_trades"],
            "win_rate": stats["win_rate"],
            "total_pnl": stats["total_pnl"],
            "exposure": exposure.total_risk_percent,
        }


def run_backtest(
    instrument: str = "EUR_USD",
    start_date: str = "2023-01-01",
    end_date: str = "2024-01-01",
    initial_balance: float = 10000.0,
    data_file: Optional[str] = None,
):
    """
    Run a backtest of the strategy.
    
    Args:
        instrument: Trading instrument
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        initial_balance: Starting balance
        data_file: Optional CSV file with historical data
    """
    logger.info(f"Starting backtest: {instrument} from {start_date} to {end_date}")
    
    # Load data
    loader = HistoricalDataLoader()
    
    if data_file and Path(data_file).exists():
        df_1h = loader.load_csv(data_file)
    else:
        logger.info("No data file provided, generating sample data")
        df_1h = loader.generate_sample_data(
            instrument=instrument,
            start=datetime.strptime(start_date, "%Y-%m-%d"),
            end=datetime.strptime(end_date, "%Y-%m-%d"),
            granularity="H1",
        )
    
    # Filter to date range
    df_1h = df_1h[start_date:end_date]
    
    if df_1h.empty:
        logger.error("No data available for the specified date range")
        return
    
    # Initialize components
    signal_generator = SignalGenerator()
    position_manager = PositionManager()
    position_sizer = PositionSizer(account_balance=initial_balance)
    stop_calculator = StopLossCalculator()
    exposure_manager = ExposureManager(account_balance=initial_balance)
    filters = TradingFilters.create_default()
    
    # Generate signals for entire dataset
    df_with_signals = signal_generator.generate_signals(df_1h)
    
    # Walk through signals
    balance = initial_balance
    
    for i in range(100, len(df_with_signals)):
        row = df_with_signals.iloc[i]
        current_time = df_with_signals.index[i]
        
        # Check stops on existing positions
        closed = position_manager.check_stops(
            instrument=instrument,
            current_high=row["high"],
            current_low=row["low"],
            current_time=current_time,
        )
        
        for pos in closed:
            balance += pos.realized_pnl
            exposure_manager.update_balance(balance)
            position_sizer.update_balance(balance)
        
        # Check for new signal
        if row["signal"] != 0 and len(position_manager.open_positions) == 0:
            signal_type = SignalType(row["signal"])
            is_long = signal_type == SignalType.BUY
            
            # Validate
            is_valid, _ = filters.validate_signal(current_time)
            if not is_valid:
                continue
            
            # Calculate stops
            window = df_with_signals.iloc[:i+1]
            stop_levels = stop_calculator.calculate_atr_stop(
                df=window,
                entry_price=row["close"],
                is_long=is_long,
            )
            
            # Calculate position size
            pos_size = position_sizer.calculate_fixed_risk(
                entry_price=row["close"],
                stop_loss=stop_levels.stop_loss,
            )
            
            # Create position
            position_manager.create_position(
                instrument=instrument,
                side=PositionSide.LONG if is_long else PositionSide.SHORT,
                units=pos_size.units,
                entry_price=row["close"],
                stop_loss=stop_levels.stop_loss,
                take_profit=stop_levels.take_profit,
                entry_time=current_time,
                signal_strength=int(row["signal_strength"]),
            )
    
    # Close any remaining positions at end
    for pos in position_manager.open_positions:
        position_manager.close_position(
            pos.id,
            exit_price=df_with_signals.iloc[-1]["close"],
            exit_time=df_with_signals.index[-1],
        )
        balance += pos.realized_pnl
    
    # Print results
    stats = position_manager.get_statistics()
    
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Instrument:      {instrument}")
    print(f"Period:          {start_date} to {end_date}")
    print(f"Initial Balance: ${initial_balance:,.2f}")
    print(f"Final Balance:   ${balance:,.2f}")
    print(f"Total Return:    {(balance/initial_balance - 1)*100:.2f}%")
    print("-" * 60)
    print(f"Total Trades:    {stats['total_trades']}")
    print(f"Winning Trades:  {stats['winning_trades']}")
    print(f"Losing Trades:   {stats['losing_trades']}")
    print(f"Win Rate:        {stats['win_rate']*100:.1f}%")
    print(f"Profit Factor:   {stats['profit_factor']:.2f}")
    print(f"Avg Win:         ${stats['avg_win']:.2f}")
    print(f"Avg Loss:        ${stats['avg_loss']:.2f}")
    print("=" * 60)
    
    # Export trades
    trades_df = position_manager.to_dataframe()
    if not trades_df.empty:
        output_file = Path("logs") / f"backtest_trades_{datetime.now():%Y%m%d_%H%M%S}.csv"
        output_file.parent.mkdir(exist_ok=True)
        trades_df.to_csv(output_file)
        print(f"\nTrades exported to: {output_file}")
    
    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Forex MTF Strategy Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run backtest")
    bt_parser.add_argument("--instrument", "-i", default="EUR_USD", help="Trading instrument")
    bt_parser.add_argument("--start", "-s", default="2023-01-01", help="Start date (YYYY-MM-DD)")
    bt_parser.add_argument("--end", "-e", default="2024-01-01", help="End date (YYYY-MM-DD)")
    bt_parser.add_argument("--balance", "-b", type=float, default=10000.0, help="Initial balance")
    bt_parser.add_argument("--data", "-d", help="Path to CSV data file")
    
    # Paper trading command
    paper_parser = subparsers.add_parser("paper", help="Run paper trading")
    paper_parser.add_argument("--instrument", "-i", default="EUR_USD", help="Trading instrument")
    
    # Live trading command
    live_parser = subparsers.add_parser("live", help="Run live trading")
    live_parser.add_argument("--instrument", "-i", default="EUR_USD", help="Trading instrument")
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Show status")
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(log_level="INFO")
    
    if args.command == "backtest":
        run_backtest(
            instrument=args.instrument,
            start_date=args.start,
            end_date=args.end,
            initial_balance=args.balance,
            data_file=args.data,
        )
    
    elif args.command == "paper":
        logger.info(f"Starting paper trading for {args.instrument}")
        bot = TradingBot(instrument=args.instrument)
        bot.run_iteration()
        print(bot.get_status())
    
    elif args.command == "live":
        logger.info(f"Starting live trading for {args.instrument}")
        logger.warning("Live trading mode - ensure OANDA credentials are configured!")
        bot = TradingBot(instrument=args.instrument)
        bot.run_iteration()
        print(bot.get_status())
    
    elif args.command == "status":
        bot = TradingBot()
        print(bot.get_status())
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
