#!/usr/bin/env python3
"""
VRVP Forex Trading Strategy - Main Entry Point

Usage:
    python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01
    python main.py paper --instrument EUR_USD
"""
import argparse
from datetime import datetime
from loguru import logger

from config import load_config
from data import OANDADataFeed, HistoricalDataLoader, MockDataFeed
from strategy import SignalGenerator, SignalType
from execution import BacktestEngine, OANDABroker
from risk import PositionSizer, ExposureManager
from monitoring import setup_logging, log_signal, log_trade

def run_backtest(args, config):
    logger.info(f"Starting backtest: {args.instrument} from {args.start} to {args.end}")

    if args.data_file:
        loader = HistoricalDataLoader()
        df = loader.load_csv(args.data_file)
        start, end = datetime.strptime(args.start, '%Y-%m-%d'), datetime.strptime(args.end, '%Y-%m-%d')
        df = loader.get_date_range(df, start, end)
        htf_df = loader.resample(df, config.trading.htf_timeframe)
    else:
        try:
            feed = OANDADataFeed(config.oanda.api_token, config.oanda.account_id, config.oanda.environment)
            data = feed.get_multi_timeframe_data(args.instrument, config.trading.timeframe, config.trading.htf_timeframe, count=5000)
            df, htf_df = data['current'], data['htf']
        except Exception as e:
            logger.warning(f"OANDA unavailable: {e}. Using mock data.")
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
    logger.info(f"Starting paper trading: {args.instrument}")
    config.oanda.environment = 'practice'

    try:
        broker = OANDABroker(config.oanda)
        feed = OANDADataFeed(config.oanda.api_token, config.oanda.account_id, 'practice')
    except Exception as e:
        logger.error(f"Failed to connect: {e}")
        return

    account = broker.get_account_info()
    logger.info(f"Account balance: ${account['balance']:,.2f}")

    signal_gen = SignalGenerator(config)
    position_sizer = PositionSizer(config.risk)
    exposure_mgr = ExposureManager(config.risk, account['balance'])
    current_position, trade_id = 0, None

    print(f"\nPaper trading {args.instrument} - Press Ctrl+C to stop\n")

    try:
        import time
        while True:
            data = feed.get_multi_timeframe_data(args.instrument, config.trading.timeframe, config.trading.htf_timeframe, count=200)
            signal = signal_gen.get_current_signal(data['current'], data['htf'], args.instrument, current_position)

            if signal.type == SignalType.LONG and current_position == 0 and exposure_mgr.can_trade():
                pos = position_sizer.calculate_position_size(account['balance'], signal.price, signal.stop_loss, args.instrument)
                result = broker.place_market_order(args.instrument, pos.units, signal.stop_loss, signal.take_profit)
                if result.success:
                    current_position, trade_id = 1, result.trade_id
                    log_trade('OPEN', args.instrument, pos.units, result.fill_price, signal.stop_loss, signal.take_profit, trade_id=trade_id)

            elif signal.type == SignalType.SHORT and current_position == 0 and exposure_mgr.can_trade():
                pos = position_sizer.calculate_position_size(account['balance'], signal.price, signal.stop_loss, args.instrument)
                result = broker.place_market_order(args.instrument, -pos.units, signal.stop_loss, signal.take_profit)
                if result.success:
                    current_position, trade_id = -1, result.trade_id
                    log_trade('OPEN', args.instrument, -pos.units, result.fill_price, signal.stop_loss, signal.take_profit, trade_id=trade_id)

            elif signal.type in [SignalType.EXIT_LONG, SignalType.EXIT_SHORT] and trade_id:
                result = broker.close_trade(trade_id)
                if result.success:
                    log_trade('CLOSE', args.instrument, 0, result.fill_price, trade_id=trade_id)
                    current_position, trade_id = 0, None

            if trade_id and not broker.get_open_trades(args.instrument):
                logger.info("Position closed by SL/TP")
                current_position, trade_id = 0, None

            account = broker.get_account_info()
            exposure_mgr.update_balance(account['balance'])
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Paper trading stopped")
        if trade_id:
            broker.close_trade(trade_id)

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
