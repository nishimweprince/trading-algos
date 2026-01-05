#!/usr/bin/env python3
"""
Backtest Example

Demonstrates how to backtest the MTF strategy with custom parameters.
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime

import pandas as pd

from config.settings import Settings, SupertrendParams, StochRSIParams
from data.historical import HistoricalDataLoader
from data.resampler import TimeframeResampler
from execution.position_manager import PositionManager, PositionSide
from indicators import (
    FVGDetector,
    StochRSIIndicator,
    SupertrendIndicator,
    VolumeProfileCalculator,
)
from monitoring.logger import setup_logging, get_logger
from risk.position_sizing import PositionSizer
from risk.stop_loss import StopLossCalculator
from strategy.signal_generator import SignalGenerator, SignalType

setup_logging(log_level="INFO")
logger = get_logger(__name__)


def run_custom_backtest():
    """Run backtest with custom indicator parameters."""
    
    print("\n" + "=" * 60)
    print("CUSTOM BACKTEST EXAMPLE")
    print("=" * 60)
    
    # Custom indicator parameters
    supertrend = SupertrendIndicator(length=14, multiplier=2.5)  # Less sensitive
    stochrsi = StochRSIIndicator(
        length=14,
        rsi_length=14,
        k=3,
        d=3,
        oversold=25,  # More conservative
        overbought=75,
    )
    fvg = FVGDetector(min_gap_pips=8.0)  # Larger gaps only
    vp = VolumeProfileCalculator(num_bins=40)
    
    # Create signal generator with custom indicators
    signal_gen = SignalGenerator(
        supertrend=supertrend,
        stochrsi=stochrsi,
        fvg_detector=fvg,
        volume_profile=vp,
    )
    
    # Load sample data
    loader = HistoricalDataLoader()
    df_1h = loader.generate_sample_data(
        instrument="EUR_USD",
        start=datetime(2023, 1, 1),
        end=datetime(2023, 12, 31),
        granularity="H1",
    )
    
    print(f"Loaded {len(df_1h)} candles")
    
    # Generate signals
    df_signals = signal_gen.generate_signals(df_1h)
    
    # Count signals
    buy_signals = (df_signals["signal"] == 1).sum()
    sell_signals = (df_signals["signal"] == -1).sum()
    
    print(f"\nSignal Summary:")
    print(f"  Buy signals:  {buy_signals}")
    print(f"  Sell signals: {sell_signals}")
    
    # Run simple backtest
    position_manager = PositionManager()
    position_sizer = PositionSizer(account_balance=10000.0)
    stop_calc = StopLossCalculator(atr_multiplier=2.0)
    
    balance = 10000.0
    trades = []
    
    for i in range(100, len(df_signals)):
        row = df_signals.iloc[i]
        current_time = df_signals.index[i]
        
        # Check stops
        closed = position_manager.check_stops(
            instrument="EUR_USD",
            current_high=row["high"],
            current_low=row["low"],
            current_time=current_time,
        )
        
        for pos in closed:
            balance += pos.realized_pnl
            trades.append({
                "entry_time": pos.entry_time,
                "exit_time": pos.exit_time,
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "pnl": pos.realized_pnl,
            })
        
        # Enter new position
        if row["signal"] != 0 and len(position_manager.open_positions) == 0:
            is_long = row["signal"] == 1
            
            # Calculate stops
            window = df_signals.iloc[:i+1]
            stops = stop_calc.calculate_atr_stop(
                df=window,
                entry_price=row["close"],
                is_long=is_long,
            )
            
            # Position size
            pos_size = position_sizer.calculate_fixed_risk(
                entry_price=row["close"],
                stop_loss=stops.stop_loss,
            )
            
            position_manager.create_position(
                instrument="EUR_USD",
                side=PositionSide.LONG if is_long else PositionSide.SHORT,
                units=pos_size.units,
                entry_price=row["close"],
                stop_loss=stops.stop_loss,
                take_profit=stops.take_profit,
                entry_time=current_time,
            )
    
    # Close remaining positions
    for pos in position_manager.open_positions:
        position_manager.close_position(
            pos.id,
            exit_price=df_signals.iloc[-1]["close"],
            exit_time=df_signals.index[-1],
        )
        balance += pos.realized_pnl
        trades.append({
            "entry_time": pos.entry_time,
            "exit_time": pos.exit_time,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "exit_price": pos.exit_price,
            "pnl": pos.realized_pnl,
        })
    
    # Results
    stats = position_manager.get_statistics()
    
    print("\n" + "-" * 60)
    print("RESULTS")
    print("-" * 60)
    print(f"Initial Balance: $10,000.00")
    print(f"Final Balance:   ${balance:,.2f}")
    print(f"Total Return:    {(balance/10000 - 1)*100:+.2f}%")
    print(f"Total Trades:    {stats['total_trades']}")
    print(f"Win Rate:        {stats['win_rate']*100:.1f}%")
    print(f"Profit Factor:   {stats['profit_factor']:.2f}")
    print("=" * 60)
    
    # Show sample trades
    if trades:
        print("\nSample Trades (first 5):")
        trades_df = pd.DataFrame(trades[:5])
        print(trades_df.to_string(index=False))


def analyze_indicators():
    """Analyze indicator values on sample data."""
    
    print("\n" + "=" * 60)
    print("INDICATOR ANALYSIS")
    print("=" * 60)
    
    # Load data
    loader = HistoricalDataLoader()
    df_1h = loader.generate_sample_data(
        instrument="EUR_USD",
        start=datetime(2023, 6, 1),
        end=datetime(2023, 6, 30),
        granularity="H1",
    )
    
    # Calculate Supertrend
    st = SupertrendIndicator(length=10, multiplier=3.0)
    st_result = st.calculate(df_1h)
    df_1h["st_direction"] = st_result.direction
    
    # Calculate StochRSI
    stoch = StochRSIIndicator()
    stoch_result = stoch.calculate(df_1h)
    df_1h["stochrsi_k"] = stoch_result.k
    
    # Detect FVGs
    fvg = FVGDetector(min_gap_pips=3.0)
    fvg_zones = fvg.detect(df_1h)
    
    # Calculate Volume Profile
    vp = VolumeProfileCalculator(num_bins=30)
    vp_result = vp.calculate(df_1h)
    
    print(f"\nData Summary:")
    print(f"  Period: {df_1h.index[0]} to {df_1h.index[-1]}")
    print(f"  Candles: {len(df_1h)}")
    
    print(f"\nSupertrend:")
    bullish_pct = (df_1h["st_direction"] == 1).mean() * 100
    print(f"  Bullish: {bullish_pct:.1f}%")
    print(f"  Bearish: {100-bullish_pct:.1f}%")
    
    print(f"\nStochRSI:")
    print(f"  Current K: {df_1h['stochrsi_k'].iloc[-1]:.1f}")
    oversold_pct = (df_1h["stochrsi_k"] < 30).mean() * 100
    print(f"  Time in Oversold: {oversold_pct:.1f}%")
    
    print(f"\nFair Value Gaps:")
    print(f"  Total FVGs: {len(fvg_zones)}")
    bullish_fvgs = sum(1 for f in fvg_zones if f.type.value == 1)
    print(f"  Bullish: {bullish_fvgs}")
    print(f"  Bearish: {len(fvg_zones) - bullish_fvgs}")
    
    print(f"\nVolume Profile:")
    print(f"  POC: {vp_result.poc:.5f}")
    print(f"  Value Area: {vp_result.value_area_low:.5f} - {vp_result.value_area_high:.5f}")
    print(f"  HVN Zones: {len(vp_result.hvn_zones)}")
    
    # Show last few rows
    print("\nLast 5 Candles:")
    display_cols = ["open", "high", "low", "close", "st_direction", "stochrsi_k"]
    print(df_1h[display_cols].tail().to_string())


if __name__ == "__main__":
    print("Running backtest example...")
    run_custom_backtest()
    
    print("\n\nRunning indicator analysis...")
    analyze_indicators()
