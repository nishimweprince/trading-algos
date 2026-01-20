# Forex Multi-Timeframe Strategy

A sophisticated forex trading system combining **4-hour Supertrend trend filtering**, **Stochastic RSI momentum detection**, **Fair Value Gap (FVG) price action analysis**, and **Volume Profile zone identification**.

## Features

- **Multi-Timeframe Analysis**: Uses 4H Supertrend for trend direction, 1H for entries
- **Smart Money Concepts**: Detects Fair Value Gaps for optimal entry zones
- **Volume Profile**: Identifies High Volume Nodes (HVN) and Point of Control (POC)
- **Risk Management**: Position sizing, exposure limits, and drawdown protection
- **OANDA Integration**: Live trading with OANDA's v20 API
- **Backtesting**: Built-in backtesting with sample data generation
- **Modular Architecture**: Clean separation of concerns for easy customization

## Installation

### Prerequisites

- Python 3.10 or higher
- OANDA demo or live account (for live trading)

### Setup

```bash
# Navigate to the strategy directory
cd forex-mtf-strategy

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` with your OANDA credentials:
```
OANDA_ACCESS_TOKEN=your-access-token-here
OANDA_ACCOUNT_ID=your-account-id-here
OANDA_ENVIRONMENT=practice
```

## Quick Start

### Run a Backtest

```bash
# Basic backtest with sample data
python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01

# With custom initial balance
python main.py backtest -i GBP_USD -s 2023-01-01 -e 2023-12-31 -b 25000

# With your own data file
python main.py backtest -i EUR_USD -d path/to/your_data.csv
```

### Paper Trading

```bash
python main.py paper --instrument EUR_USD
```

### Live Trading

```bash
# Ensure OANDA credentials are configured
python main.py live --instrument EUR_USD
```

## Strategy Logic

### Entry Conditions

**Long Entry:**
1. 4H Supertrend direction is bullish (direction = 1)
2. 1H StochRSI crosses up from oversold zone (< 30)
3. Price is near a bullish FVG zone OR near HVN/POC

**Short Entry:**
1. 4H Supertrend direction is bearish (direction = -1)
2. 1H StochRSI crosses down from overbought zone (> 70)
3. Price is near a bearish FVG zone OR near HVN/POC

### Risk Management

- **Position Sizing**: 2% risk per trade (configurable)
- **Stop Loss**: ATR-based, structure-based, or FVG-based
- **Take Profit**: Default 2:1 risk/reward ratio
- **Exposure Limits**: Maximum 6% total exposure, max 5 positions

## Project Structure

```
forex-mtf-strategy/
├── config/
│   ├── settings.py          # Configuration and parameters
│   └── instruments.yaml     # Instrument specifications
├── data/
│   ├── feed.py              # OANDA data streaming/polling
│   ├── historical.py        # Historical data loading
│   └── resampler.py         # Multi-timeframe resampling
├── indicators/
│   ├── supertrend.py        # Supertrend indicator
│   ├── stochrsi.py          # Stochastic RSI
│   ├── fvg.py               # Fair Value Gap detection
│   └── volume_profile.py    # Volume Profile (VRVP)
├── strategy/
│   ├── signal_generator.py  # Signal generation logic
│   └── filters.py           # Trading filters (time, spread)
├── execution/
│   ├── broker.py            # OANDA order execution
│   └── position_manager.py  # Position tracking
├── risk/
│   ├── position_sizing.py   # Position size calculation
│   ├── stop_loss.py         # Stop loss methods
│   └── exposure.py          # Portfolio exposure management
├── monitoring/
│   ├── logger.py            # Logging configuration
│   └── alerts.py            # Telegram/console alerts
├── examples/
│   ├── backtest_example.py  # Backtest demonstration
│   └── live_trading_example.py  # Live trading demo
├── main.py                  # Main entry point
└── requirements.txt         # Dependencies
```

## Customization

### Indicator Parameters

Edit `config/settings.py` or pass custom parameters:

```python
from indicators import SupertrendIndicator, StochRSIIndicator

# Custom Supertrend
supertrend = SupertrendIndicator(length=14, multiplier=2.5)

# Custom StochRSI
stochrsi = StochRSIIndicator(
    length=14,
    rsi_length=14,
    k=3,
    d=3,
    oversold=25,
    overbought=75,
)

# Use in signal generator
from strategy.signal_generator import SignalGenerator
signal_gen = SignalGenerator(
    supertrend=supertrend,
    stochrsi=stochrsi,
)
```

### Risk Parameters

```python
from risk.position_sizing import PositionSizer
from risk.exposure import ExposureManager

# Conservative settings
sizer = PositionSizer(
    account_balance=10000,
    max_risk_pct=0.01,  # 1% per trade
    max_position_size=50000,  # 0.5 lot max
)

exposure = ExposureManager(
    account_balance=10000,
    max_total_exposure=0.04,  # 4% max total exposure
    max_positions_per_pair=1,
    max_total_positions=3,
    max_drawdown_pct=0.10,  # 10% max drawdown
)
```

### Trading Filters

```python
from strategy.filters import TradingFilters, TimeFilter, LONDON_NY_OVERLAP

# Only trade during London-NY overlap
strict_filters = TradingFilters(
    time_filter=TimeFilter(
        allowed_sessions=[LONDON_NY_OVERLAP],
        excluded_days=[4, 5, 6],  # No Friday-Sunday
    ),
)
```

## Data Sources

### Historical Data

The strategy supports multiple data formats:

1. **Dukascopy** (recommended for backtesting):
```bash
npx dukascopy-node -i eurusd -from 2023-01-01 -to 2024-01-01 -t h1 -f csv
```

2. **Sample Data** (for testing):
```python
from data.historical import HistoricalDataLoader
loader = HistoricalDataLoader()
df = loader.generate_sample_data(
    instrument="EUR_USD",
    start=datetime(2023, 1, 1),
    end=datetime(2024, 1, 1),
    granularity="H1",
)
```

3. **OANDA API** (for live/paper):
```python
from data.feed import OANDADataFeed
feed = OANDADataFeed()
df = feed.get_candles("EUR_USD", granularity="H1", count=500)
```

## Alerts

### Telegram Notifications

Configure Telegram alerts in `.env`:

```
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

Alerts are sent for:
- New signals
- Trade entries/exits
- Daily summaries
- Errors

### Console Alerts

Always enabled, showing real-time trading activity.

## Important Notes

### Look-Ahead Bias Prevention

The strategy properly shifts higher timeframe data to prevent look-ahead bias:

```python
# 4H data is shifted by 1 candle before merging to 1H
df_4h['st_direction'] = st_result.direction.shift(1)
```

### Forex Volume

Forex markets don't have centralized volume. This strategy uses **tick volume** as a proxy, which still provides useful distribution information for Volume Profile analysis.

### Paper Trading First

Always test with OANDA's practice account before live trading:

```
OANDA_ENVIRONMENT=practice
```

## License

MIT License - See LICENSE file for details.

## Disclaimer

This software is for educational purposes only. Trading forex involves substantial risk of loss. Past performance is not indicative of future results. Always use proper risk management and never trade with money you cannot afford to lose.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Submit a pull request with tests

## Resources

- [pandas-ta Documentation](https://github.com/twopirllc/pandas-ta)
- [smartmoneyconcepts Package](https://github.com/joshyattridge/smart-money-concepts)
- [OANDA v20 API](https://developer.oanda.com/rest-live-v20/introduction/)
- [Dukascopy Historical Data](https://www.dukascopy.com/trading-tools/widgets/quotes/historical_data_feed)
