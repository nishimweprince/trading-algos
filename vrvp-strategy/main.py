#!/usr/bin/env python3
"""
VRVP Forex Trading Strategy - Main Entry Point

Usage:
    python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01
    python main.py paper --instrument EUR_USD
"""
import sys
import os
from pathlib import Path

# Add project root to Python path to enable imports
project_root = Path(__file__).parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Ensure we can import as a package
os.chdir(project_root)

import argparse
from datetime import datetime
from typing import Dict
import pandas as pd
from loguru import logger

from config import load_config
from data import HistoricalDataLoader, MockDataFeed, CapitalDataFeed, ForexDataScheduler
from strategy import SignalGenerator, SignalType
from execution import BacktestEngine
from monitoring import setup_logging, log_signal

def run_backtest(args, config):
    logger.info(f"Starting backtest: {args.instrument} from {args.start} to {args.end}")

    if args.data_file:
        loader = HistoricalDataLoader()
        df = loader.load_csv(args.data_file)
        start, end = datetime.strptime(args.start, '%Y-%m-%d'), datetime.strptime(args.end, '%Y-%m-%d')
        df = loader.get_date_range(df, start, end)

        # Detect CSV timeframe and resample if needed
        if len(df) > 1:
            time_diff = df.index[1] - df.index[0]
            csv_tf_seconds = time_diff.total_seconds()
            expected_tf_seconds = {'1M': 60, '5M': 300, '15M': 900, '30M': 1800, '1H': 3600, '4H': 14400, '1D': 86400}.get(config.trading.timeframe, 3600)

            # If CSV is a lower timeframe than expected, resample up
            if csv_tf_seconds < expected_tf_seconds:
                logger.info(f"Resampling CSV data from {csv_tf_seconds}s to {config.trading.timeframe} ({expected_tf_seconds}s)")
                df = loader.resample(df, config.trading.timeframe)
            elif abs(csv_tf_seconds - expected_tf_seconds) > 60:  # Allow 1 minute tolerance
                logger.warning(f"CSV data timeframe ({csv_tf_seconds}s) doesn't match expected timeframe ({config.trading.timeframe}, {expected_tf_seconds}s). Strategy may not work as expected.")

        htf_df = loader.resample(df, config.trading.htf_timeframe)
    else:
        # Try Capital.com API first, fallback to mock data
        try:
            capitalcom_issues = config.capitalcom.validate()
            if not capitalcom_issues:
                feed = CapitalDataFeed(
                    api_key=config.capitalcom.api_key,
                    password=config.capitalcom.api_password,
                    username=config.capitalcom.username,
                    environment=config.capitalcom.environment
                )
                data = feed.get_multi_timeframe_data(args.instrument, config.trading.timeframe, config.trading.htf_timeframe, count=5000)
                df, htf_df = data['current'], data['htf']
            else:
                raise Exception("Capital.com API not configured")
        except Exception as e:
            logger.warning(f"Capital.com API unavailable: {e}. Using mock data.")
            feed = MockDataFeed()
            data = feed.get_multi_timeframe_data(args.instrument)
            df, htf_df = data['current'], data['htf']

    if len(df) == 0:
        logger.error("No data available")
        return

    engine = BacktestEngine(config)
    result = engine.run(df, htf_df, args.instrument, args.balance)

    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Instrument: {args.instrument}")
    print(f"Initial Balance: ${result.initial_balance:,.2f}")
    print(f"Final Balance: ${result.final_balance:,.2f}")
    print(f"Total Return: {result.total_return_pct:.2f}%")
    print("-" * 60)
    print(f"Total Trades: {result.total_trades}")
    print(f"Win Rate: {result.win_rate:.1f}%")
    print(f"Profit Factor: {result.profit_factor:.2f}")
    print(f"Max Drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print("=" * 60)

    if args.output:
        import pandas as pd
        trades_df = pd.DataFrame([{'entry_time': t.entry_time, 'exit_time': t.exit_time, 'direction': 'LONG' if t.direction == 1 else 'SHORT',
                                   'units': t.units, 'entry_price': t.entry_price, 'exit_price': t.exit_price, 'pnl': t.pnl, 'exit_reason': t.exit_reason}
                                  for t in result.trades])
        trades_df.to_csv(args.output, index=False)
        print(f"\nTrades saved to: {args.output}")

def run_paper(args, config):
    """Run signal generation with Capital.com API - logs signals to console (no trading)"""
    logger.info(f"Starting signal generation (Capital.com API): {args.instrument}")

    # Validate Capital.com API configuration
    capitalcom_issues = config.capitalcom.validate()
    if capitalcom_issues:
        logger.error("Capital.com API configuration issues:")
        for issue in capitalcom_issues:
            logger.error(f"  - {issue}")
        logger.error("Please set CAPITALCOM_API_KEY, CAPITALCOM_API_PASSWORD, and CAPITALCOM_USERNAME in your .env file")
        return

    try:
        # Initialize Capital.com API data feed
        feed = CapitalDataFeed(
            api_key=config.capitalcom.api_key,
            password=config.capitalcom.api_password,
            username=config.capitalcom.username,
            environment=config.capitalcom.environment
        )

        # CRITICAL: Authenticate BEFORE creating scheduler
        logger.info("Authenticating with Capital.com...")
        print("\n" + "="*60)
        print("Authenticating with Capital.com...")
        print(f"Environment: {config.capitalcom.environment}")
        print("="*60 + "\n")

        if not feed.authenticate():
            logger.error("Authentication failed")
            print("\n" + "="*60)
            print("❌ AUTHENTICATION FAILED")
            print("="*60)
            print("\nPossible reasons:")
            print("1. Invalid credentials (check .env file)")
            print("2. API key environment mismatch (demo vs live)")
            print("3. API key lacks 'Trading' permissions")
            print("4. Rate limited (wait 5 minutes)")
            print("\nCheck logs for specific error details.")
            print("="*60 + "\n")
            return  # EXIT - don't start scheduler

        logger.info("✓ Authentication successful!")
        print("✓ Authentication successful!\n")

        # Initialize strategy components
        signal_gen = SignalGenerator(config)

        # Create scheduler with 60-second interval
        instruments = [args.instrument]
        timeframes = [config.trading.timeframe, config.trading.htf_timeframe]
        scheduler = ForexDataScheduler(
            feed=feed,
            instruments=instruments,
            timeframes=timeframes,
            fetch_interval_seconds=60
        )

        # Define callback for new data
        def on_new_data(results: Dict[str, Dict[str, pd.DataFrame]]):
            if args.instrument not in results:
                logger.warning(f"No data for {args.instrument} in results")
                return

            # Get cached data for both timeframes
            ltf_df = scheduler.get_cached_data(args.instrument, config.trading.timeframe)
            htf_df = scheduler.get_cached_data(args.instrument, config.trading.htf_timeframe)

            if ltf_df is None or htf_df is None:
                logger.warning(f"Incomplete data for {args.instrument}")
                return

            if len(ltf_df) == 0 or len(htf_df) == 0:
                logger.warning(f"Empty dataframes for {args.instrument}")
                return

            logger.info(f"New data received: {len(ltf_df)} LTF candles, {len(htf_df)} HTF candles")

            try:
                # Generate signal (no position tracking for signal-only mode)
                signal = signal_gen.get_current_signal(
                    ltf_df,
                    htf_df,
                    args.instrument,
                    current_position=0  # Always assume no position for signal generation
                )

                # Log signal to console
                signal_type_str = signal.type.name if isinstance(signal.type, SignalType) else str(signal.type)

                print("\n" + "=" * 60)
                print(f"SIGNAL GENERATED: {signal_type_str}")
                print("=" * 60)
                print(f"Instrument: {args.instrument}")
                print(f"Signal Type: {signal_type_str}")
                print(f"Price: {signal.price:.5f}")
                print(f"Strength: {signal.strength:.2f}")

                if signal.stop_loss:
                    print(f"Stop Loss: {signal.stop_loss:.5f}")
                if signal.take_profit:
                    print(f"Take Profit: {signal.take_profit:.5f}")
                if signal.reasons:
                    print(f"Reasons: {', '.join(signal.reasons)}")

                print(f"Timestamp: {pd.Timestamp.now()}")
                print("=" * 60 + "\n")

                # Also log via logger
                logger.info(f"SIGNAL: {signal_type_str} for {args.instrument} at {signal.price:.5f} (strength: {signal.strength:.2f})")

            except Exception as e:
                logger.error(f"Error processing signal: {e}")
                import traceback
                logger.error(traceback.format_exc())

        # Register callback
        scheduler.on_data_fetched(on_new_data)

        # Start scheduler
        scheduler.start()

        print(f"\n{'=' * 60}")
        print(f"Signal Generation Mode - {args.instrument}")
        print(f"{'=' * 60}")
        print(f"Data Source: Capital.com API")
        print(f"Environment: {config.capitalcom.environment}")
        print(f"Fetch Interval: 60 seconds")
        print(f"Timeframes: {config.trading.timeframe} (LTF), {config.trading.htf_timeframe} (HTF)")
        print(f"\nSignals will be logged to console as they are generated.")
        print(f"Press Ctrl+C to stop\n")

        # Keep-alive loop with health checks
        try:
            import time
            while True:
                time.sleep(60)  # Health check every minute

                # Check if scheduler is still running
                if not scheduler.is_running():
                    logger.error("Scheduler stopped unexpectedly!")
                    break

                # Check authentication status every minute
                if not feed.is_authenticated:
                    logger.warning("Lost authentication, re-authenticating...")
                    if not feed.authenticate():
                        logger.error("Re-authentication failed, stopping")
                        break

        except KeyboardInterrupt:
            logger.info("Signal generation stopped by user")
            scheduler.stop()
            feed.logout()

    except Exception as e:
        logger.error(f"Failed to start signal generation: {e}")
        import traceback
        logger.error(traceback.format_exc())

def main():
    parser = argparse.ArgumentParser(description='VRVP Forex Trading Strategy')
    subparsers = parser.add_subparsers(dest='command')

    bt = subparsers.add_parser('backtest')
    bt.add_argument('--instrument', '-i', default='EUR_USD')
    bt.add_argument('--start', '-s', default='2023-01-01')
    bt.add_argument('--end', '-e', default='2024-01-01')
    bt.add_argument('--balance', '-b', type=float, default=10000)
    bt.add_argument('--data-file', '-d')
    bt.add_argument('--output', '-o')

    paper = subparsers.add_parser('paper')
    paper.add_argument('--instrument', '-i', default='EUR_USD')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    config = load_config()
    setup_logging(config.logging)

    if args.command == 'backtest':
        run_backtest(args, config)
    elif args.command == 'paper':
        run_paper(args, config)

if __name__ == '__main__':
    main()
