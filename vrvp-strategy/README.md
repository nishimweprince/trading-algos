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
# On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure Capital.com API credentials
# Create .env file in project root with:
# CAPITALCOM_API_KEY=your_api_key
# CAPITALCOM_API_PASSWORD=your_api_password
# CAPITALCOM_USERNAME=your_username  # REQUIRED: Your Capital.com login username/email
# CAPITALCOM_ENVIRONMENT=demo  # or 'live' for production
```

## Quick Start

### Backtesting

```bash
# Basic backtest with default dates
python main.py backtest --instrument EUR_USD

# Backtest with custom date range
python main.py backtest --instrument EUR_USD --start 2023-01-01 --end 2024-01-01

# Backtest with CSV file
python main.py backtest -i EUR_USD -d data/historical/EURUSD_H1.csv -o trades.csv

# Backtest with custom initial balance
python main.py backtest -i GBP_USD -s 2023-06-01 -e 2023-12-31 -b 50000
```

### Paper Trading (Signal Generation)

```bash
# Single currency pair
python main.py paper --instrument EUR_USD

# Using short flags
python main.py paper -i GBP_USD
```

### Running Multiple Currencies

To run paper trading for multiple currencies simultaneously, you can:

**Option 1: Multiple Terminal Windows (Recommended)**
```bash
# Terminal 1
python main.py paper -i EUR_USD

# Terminal 2
python main.py paper -i GBP_USD

# Terminal 3
python main.py paper -i USD_JPY
```

**Option 2: Background Processes (Linux/Mac)**
```bash
# Run in background
nohup python main.py paper -i EUR_USD > eurusd.log 2>&1 &
nohup python main.py paper -i GBP_USD > gbpusd.log 2>&1 &
nohup python main.py paper -i USD_JPY > usdjpy.log 2>&1 &

# Check running processes
ps aux | grep "python main.py paper"

# Stop a process
kill <PID>
```

**Option 3: Using Screen/Tmux (Linux/Mac)**
```bash
# Create a screen session for each currency
screen -S eurusd
python main.py paper -i EUR_USD
# Press Ctrl+A then D to detach

screen -S gbpusd
python main.py paper -i GBP_USD
# Press Ctrl+A then D to detach

# Reattach to a session
screen -r eurusd
```

## Command Reference

### Backtest Command

```bash
python main.py backtest [OPTIONS]
```

**Options:**
- `--instrument, -i`: Currency pair (default: `EUR_USD`)
- `--start, -s`: Start date in YYYY-MM-DD format (default: `2023-01-01`)
- `--end, -e`: End date in YYYY-MM-DD format (default: `2024-01-01`)
- `--balance, -b`: Initial balance for backtest (default: `10000`)
- `--data-file, -d`: Path to CSV file with historical data (optional)
- `--output, -o`: Output CSV file path for trade results (optional)

**Examples:**
```bash
# Full backtest with all options
python main.py backtest -i EUR_USD -s 2023-01-01 -e 2024-01-01 -b 25000 -o results.csv

# Backtest from CSV file
python main.py backtest -i EUR_USD -d data/historical/EURUSD_H1.csv -o trades.csv

# Quick backtest with defaults
python main.py backtest
```

### Paper Trading Command

```bash
python main.py paper [OPTIONS]
```

**Options:**
- `--instrument, -i`: Currency pair to monitor (default: `EUR_USD`)

**Examples:**
```bash
# Monitor EUR/USD
python main.py paper -i EUR_USD

# Monitor GBP/USD
python main.py paper --instrument GBP_USD
```

**Note:** Paper trading mode generates and logs trading signals but does not execute trades. It fetches real-time data from Capital.com API at a configurable interval (default: 5 minutes) and displays signals in the console. Set `FETCH_INTERVAL_MINUTES` in your `.env` file to customize the interval.

### Alternative Execution Methods

You can also run the strategy using:

```bash
# Using run.py wrapper
python run.py backtest -i EUR_USD

# As a Python module
python -m __main__ paper -i GBP_USD
```

## Supported Instruments

The strategy supports the following currency pairs (standard format with underscore):

- **Major Pairs**: `EUR_USD`, `GBP_USD`, `USD_JPY`, `USD_CHF`, `AUD_USD`, `USD_CAD`, `NZD_USD`
- **Cross Pairs**: `EUR_GBP`, `EUR_JPY`, `GBP_JPY`, `AUD_JPY`, `EUR_CHF`, `AUD_NZD`, `EUR_AUD`, `GBP_AUD`

**Note:** The instrument mapper automatically converts between formats:
- Standard format: `EUR_USD` (with underscore)
- Capital.com epic: `EURUSD` (no separator)

You can also use the 6-character format directly: `EURUSD`, `GBPUSD`, etc.

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

## Configuration

### Environment Variables (.env file)

Create a `.env` file in the project root with the following variables:

```bash
# Capital.com API Configuration (REQUIRED for paper trading)
CAPITALCOM_API_KEY=your_api_key_here
CAPITALCOM_API_PASSWORD=your_api_password_here
CAPITALCOM_USERNAME=your_capital_com_username_or_email  # REQUIRED
CAPITALCOM_ENVIRONMENT=demo  # 'demo' or 'live'

# Optional: Risk Management
RISK_PER_TRADE=2.0  # Risk per trade as percentage
MAX_DRAWDOWN=15.0   # Maximum drawdown percentage

# Optional: Trading Settings
INSTRUMENTS=EUR_USD,GBP_USD,USD_JPY  # Comma-separated list
TIMEFRAME=1H  # Lower timeframe (default: 1H)
FETCH_INTERVAL_MINUTES=5  # Scheduler fetch interval in minutes (default: 5)

# Optional: Logging
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### Configuration Parameters

All configuration can be set via environment variables or defaults in `config/settings.py`:

| Category | Parameter | Default | Description |
|----------|-----------|---------|-------------|
| **Supertrend** | `period` | 10 | ATR period for Supertrend |
| | `multiplier` | 3.0 | ATR multiplier |
| | `source` | 'hl2' | Price source (hl2 = (high+low)/2) |
| **StochRSI** | `rsi_period` | 14 | RSI period |
| | `stoch_period` | 14 | Stochastic period |
| | `k_smooth` | 3 | K smoothing |
| | `d_smooth` | 3 | D smoothing |
| | `oversold` | 20.0 | Oversold threshold |
| | `overbought` | 80.0 | Overbought threshold |
| **Volume Profile** | `lookback_periods` | 100 | Number of periods for VP calculation |
| | `num_bins` | 50 | Number of price bins |
| | `value_area_pct` | 0.70 | Value area percentage (70%) |
| | `proximity_atr_mult` | 1.0 | Proximity multiplier for support/resistance |
| **FVG** | `max_zones` | 20 | Maximum FVG zones to track |
| | `min_gap_atr_mult` | 0.1 | Minimum gap size (ATR multiplier) |
| **Risk** | `risk_per_trade_pct` | 2.0 | Risk per trade (%) |
| | `max_position_pct` | 10.0 | Maximum position size (%) |
| | `max_drawdown_pct` | 15.0 | Circuit breaker threshold (%) |
| | `stop_loss_atr_mult` | 2.0 | Stop loss ATR multiplier |
| | `take_profit_atr_mult` | 4.0 | Take profit ATR multiplier |
| | `min_risk_reward` | 1.5 | Minimum risk:reward ratio |
| | `breakeven_trigger_pct` | 1.0 | Profit % to trigger break-even |
| **Trading** | `timeframe` | '1H' | Lower timeframe |
| | `htf_timeframe` | '4H' | Higher timeframe |
| | `min_candles_between_trades` | 2 | Minimum candles between trades |
| | `trading_hours_start` | 0 | Trading hours start (24h format) |
| | `trading_hours_end` | 24 | Trading hours end (24h format) |
| | `fetch_interval_minutes` | 5 | Scheduler fetch interval in minutes (paper trading) |
| **Backtest** | `initial_capital` | 10000.0 | Initial capital for backtest |
| | `commission_pct` | 0.0 | Commission percentage |
| | `spread_pips` | 1.5 | Spread in pips |
| **Logging** | `level` | 'INFO' | Log level |
| | `log_file` | 'logs/vrvp_strategy.log' | Log file path |
| | `log_trades` | True | Log trades |
| | `log_signals` | True | Log signals |

## Risk Management

- **Position Sizing**: Fixed fractional (2% risk per trade)
- **Max Position**: 10% of balance per position
- **Circuit Breaker**: Trading halts at 15% drawdown
- **Break-Even Stop**: Moves after 1% profit
- **ATR-Based Stops**: Dynamic stop loss based on volatility

## Multi-Timeframe Handling

**CRITICAL**: HTF data is shifted by 1 bar to prevent look-ahead bias:
```python
htf_df['st_trend'] = htf_df['st_trend'].shift(1)
ltf_df['trend_htf'] = htf_df['st_trend'].reindex(ltf_df.index, method='ffill')
```

This ensures that higher timeframe signals are only available after the HTF candle closes, preventing future data leakage.

## Project Structure

```
vrvp-strategy/
├── config/              # Strategy configuration
│   ├── __init__.py
│   └── settings.py     # Configuration classes and loading
├── data/               # Data feed and processing
│   ├── __init__.py
│   ├── dto.py          # Data Transfer Objects (normalized structures)
│   ├── dto_transformers.py  # API response transformers
│   ├── capital_client.py   # Capital.com REST API client
│   ├── capital_feed.py     # Capital.com data feed wrapper
│   ├── instrument_mapper.py  # Instrument name mapping
│   ├── historical.py   # CSV historical data loader
│   ├── mock_feed.py    # Mock data feed for testing
│   ├── resampler.py    # Timeframe resampling utilities
│   └── scheduler.py    # APScheduler-based data fetching
├── indicators/         # Technical indicators
│   ├── __init__.py
│   ├── calculator.py   # Indicator calculation utilities
│   ├── fvg.py          # Fair Value Gap detection
│   ├── stochrsi.py     # Stochastic RSI
│   ├── supertrend.py   # Supertrend indicator
│   └── volume_profile.py  # Volume Profile calculations
├── strategy/           # Strategy logic
│   ├── __init__.py
│   └── signal_generator.py  # Signal generation logic
├── execution/          # Trade execution
│   ├── __init__.py
│   └── backtest.py     # Backtesting engine
├── risk/               # Risk management
│   ├── __init__.py
│   ├── exposure.py     # Exposure management
│   ├── position_sizing.py  # Position sizing calculations
│   └── stop_manager.py     # Stop loss management
├── monitoring/         # Logging and monitoring
│   ├── __init__.py
│   └── logger.py       # Logging setup
├── logs/               # Log files
├── data/historical/    # Historical CSV data files
├── main.py             # Main entry point
├── run.py              # Wrapper script
├── __main__.py         # Module entry point
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

## Capital.com API Setup

### 1. Generate API Key

1. Log in to your Capital.com account
2. Navigate to **Settings** > **API integrations**
3. Click **Generate API key**
4. Enable Two-Factor Authentication (2FA) if not already enabled
5. Provide a name, set a password, and optionally set expiration date
6. Enter your 2FA code to finalize
7. **Save the API key and password securely** (shown only once)

### 2. Configure Environment Variables

Create a `.env` file in the project root:

```bash
CAPITALCOM_API_KEY=your_api_key_here
CAPITALCOM_API_PASSWORD=your_api_password_here
CAPITALCOM_USERNAME=your_capital_com_username_or_email  # REQUIRED
CAPITALCOM_ENVIRONMENT=demo  # Use 'demo' for paper trading, 'live' for production
```

**Important**: 
- `CAPITALCOM_USERNAME` is **required** and must be your Capital.com account login username or email address
- The API key alone will not work for authentication
- Use `demo` environment for testing and paper trading
- Use `live` environment only when ready for real trading

### 3. Instrument Format

- The strategy uses standard format (e.g., `EUR_USD`)
- Capital.com uses "epics" (e.g., `EURUSD`)
- Automatic conversion is handled by the `InstrumentMapper` class
- Both formats are accepted: `EUR_USD` or `EURUSD`

## DTO Architecture

The project uses a **Data Transfer Object (DTO)** normalization layer that:
- Provides source-agnostic data structures (`CandleDTO`, `PriceDTO`, `AccountDTO`, etc.)
- Makes it easy to add new data sources by implementing transformers
- Keeps strategy code independent of API-specific formats
- Enables easy testing with mock DTOs

To add a new broker/data source, implement `BaseDTOTransformer` and create corresponding client classes.

## Backtest Results

When running a backtest, you'll see output like:

```
============================================================
BACKTEST RESULTS
============================================================
Instrument: EUR_USD
Initial Balance: $10,000.00
Final Balance: $12,450.00
Total Return: 24.50%
------------------------------------------------------------
Total Trades: 45
Win Rate: 55.6%
Profit Factor: 1.85
Max Drawdown: 8.20%
Sharpe Ratio: 1.42
============================================================
```

If you specify `--output`, trade details will be saved to a CSV file with columns:
- `entry_time`: Entry timestamp
- `exit_time`: Exit timestamp
- `direction`: LONG or SHORT
- `units`: Position size
- `entry_price`: Entry price
- `exit_price`: Exit price
- `pnl`: Profit/Loss
- `exit_reason`: Reason for exit

## Paper Trading Output

When running paper trading, you'll see real-time signals like:

```
============================================================
SIGNAL GENERATED: LONG
============================================================
Instrument: EUR_USD
Signal Type: LONG
Price: 1.08542
Strength: 0.85
Stop Loss: 1.08320
Take Profit: 1.08986
Reasons: Supertrend uptrend, StochRSI oversold bounce, Near VP POC
Timestamp: 2024-01-15 14:30:00
============================================================
```

Signals are generated at the configured fetch interval (default: 5 minutes) when new candle data is available. Configure via `FETCH_INTERVAL_MINUTES` in `.env`.

## Troubleshooting

### Authentication Failed

If you see "AUTHENTICATION FAILED":
1. Verify your `.env` file has all three required variables
2. Check that `CAPITALCOM_USERNAME` matches your Capital.com login
3. Ensure API key environment matches (`demo` vs `live`)
4. Verify API key has "Trading" permissions
5. Wait 5 minutes if rate limited

### No Data Available

If backtest shows "No data available":
1. Check CSV file path is correct
2. Verify CSV has required columns: `timestamp`, `open`, `high`, `low`, `close`, `volume`
3. Ensure date range has data
4. Check Capital.com API connection for live data

### Scheduler Stopped Unexpectedly

If paper trading stops:
1. Check logs in `logs/vrvp_strategy.log`
2. Verify API authentication is still valid
3. Check network connectivity
4. Restart the process

### Import Errors

If you see import errors:
1. Ensure virtual environment is activated
2. Run `pip install -r requirements.txt`
3. Verify you're in the project root directory
4. Check Python version (3.8+ required)

## Dependencies

- **pandas-ta**: Supertrend, StochRSI indicators
- **smartmoneyconcepts**: FVG detection (optional, fallback implementation included)
- **MarketProfile**: Volume Profile calculations
- **requests**: HTTP client for Capital.com REST API
- **websocket-client**: WebSocket client for real-time market data
- **loguru**: Advanced logging
- **apscheduler**: Scheduled data fetching
- **pandas**: Data manipulation
- **numpy**: Numerical computations
- **python-dotenv**: Environment variable management

See `requirements.txt` for complete list with versions.

## Logging

Logs are written to `logs/vrvp_strategy.log` by default. Log level can be controlled via:
- Environment variable: `LOG_LEVEL=DEBUG`
- Or edit `config/settings.py`

Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

## Performance Considerations

- **Data Fetching**: Paper trading fetches data at configurable interval (default: 5 minutes, set via `FETCH_INTERVAL_MINUTES`)
- **Multiple Instruments**: Each instrument runs in a separate process
- **Memory Usage**: Caches last 200 candles per instrument/timeframe
- **API Rate Limits**: Capital.com has rate limits; scheduler handles retries

## Disclaimer

This software is for educational purposes only. Trading forex involves significant risk of loss. Always:
- Test thoroughly with paper trading before live trading
- Understand the strategy logic and risks
- Never risk more than you can afford to lose
- Consider consulting with a financial advisor
- Past performance does not guarantee future results

## License

This project is provided as-is for educational purposes.
