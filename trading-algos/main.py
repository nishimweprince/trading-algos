import argparse
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from backtest import run_backtest
from strategy.signal_generator import SignalGenerator
from data.historical import generate_dummy_data

def main():
    parser = argparse.ArgumentParser(description="Forex Trading Strategy Bot")
    parser.add_argument('mode', choices=['backtest', 'live', 'generate_data'], help='Mode to run')
    
    args = parser.parse_args()
    
    if args.mode == 'backtest':
        print("Running Backtest...")
        run_backtest()
    elif args.mode == 'generate_data':
        print("Generating dummy data...")
        generate_dummy_data(days=100).to_csv('dummy_data.csv')
        print("Done.")
    elif args.mode == 'live':
        print("Live mode not fully implemented (requires valid API keys).")
        print("Structure is ready in execution/broker.py")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
