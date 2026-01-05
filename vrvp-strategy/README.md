# VRVP Forex Trading Strategy

A sophisticated multi-timeframe forex trading system combining **Volume Profile**, **Stochastic RSI**, **Fair Value Gap (FVG)**, and **Supertrend** indicators.

## Overview

| Component | Timeframe | Purpose | Library |
|-----------|-----------|---------|---------|
| **Supertrend** | 4-Hour | Trend filtering | pandas-ta |
| **Stochastic RSI** | 1-Hour | Momentum detection | pandas-ta |
| **Fair Value Gap** | 1-Hour | Price imbalance zones | smartmoneyconcepts |
| **Volume Profile** | 1-Hour | POC, VAH, VAL, HVN/LVN | Custom/MarketProfile |

## Installation

```bash
cd trading-algos/vrvp-strategy
python -m venv venv
source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt

# Configure OANDA credentials
cp .env.example .env
# Edit .env with your API credentials
```

## Quick Start

### Backtesting
```bash
python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01
python main.py backtest -i EUR_USD -d data/historical/EURUSD_H1.csv -o trades.csv
```

### Paper Trading
```bash
python main.py paper --instrument EUR_USD
```

## Strategy Logic

### Long Entry (all required):
1. **4H Supertrend**: Uptrend (trend = 1)
2. **StochRSI**: Crossing above oversold OR moving from oversold (K < 60)
3. **Confluence**: Bouncing off bullish FVG OR near VP support (POC/VAL)
4. **Filter**: NOT in Low Volume Node (LVN) zone

### Short Entry (all required):
1. **4H Supertrend**: Downtrend (trend = -1)
2. **StochRSI**: Crossing below overbought OR moving from overbought (K > 40)
3. **Confluence**: Bouncing off bearish FVG OR near VP resistance (POC/VAH)
4. **Filter**: NOT in Low Volume Node (LVN) zone

### Exit Conditions
- **Stop Loss**: ATR-based (default 2x ATR)
- **Take Profit**: ATR-based (default 4x ATR, 2:1 R:R)
- **Trailing**: Move to break-even after 1% profit
- **Signal Exit**: Supertrend reversal or extreme StochRSI

## Project Structure

```
vrvp-strategy/
├── config/          # Strategy configuration
├── data/            # OANDA feed, CSV loader, resampler
├── indicators/      # Supertrend, StochRSI, FVG, Volume Profile
├── strategy/        # Signal generation
├── execution/       # OANDA broker, backtesting
├── risk/            # Position sizing, stop management, exposure
├── monitoring/      # Logging
├── main.py          # Entry point
└── requirements.txt
```

## Configuration

Edit `.env` or `config/settings.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `supertrend_period` | 10 | ATR period for Supertrend |
| `supertrend_multiplier` | 3.0 | ATR multiplier |
| `stochrsi_oversold` | 20 | Oversold threshold |
| `stochrsi_overbought` | 80 | Overbought threshold |
| `stop_loss_atr_mult` | 2.0 | Stop loss ATR multiplier |
| `take_profit_atr_mult` | 4.0 | Take profit ATR multiplier |
| `risk_per_trade_pct` | 2.0 | Risk per trade (%) |
| `max_drawdown_pct` | 15.0 | Circuit breaker threshold |

## Risk Management

- **Position Sizing**: Fixed fractional (2% risk per trade)
- **Max Position**: 10% of balance per position
- **Circuit Breaker**: Trading halts at 15% drawdown
- **Break-Even Stop**: Moves after 1% profit

## Multi-Timeframe Handling

**CRITICAL**: HTF data is shifted by 1 bar to prevent look-ahead bias:
```python
htf_df['st_trend'] = htf_df['st_trend'].shift(1)
ltf_df['trend_htf'] = htf_df['st_trend'].reindex(ltf_df.index, method='ffill')
```

## Dependencies

- **pandas-ta**: Supertrend, StochRSI
- **smartmoneyconcepts**: FVG detection
- **oandapyV20**: OANDA API
- **loguru**: Logging

## Disclaimer

This software is for educational purposes only. Trading forex involves significant risk. Always test with paper trading first.
