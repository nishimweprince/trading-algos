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

# Configure Capital.com API credentials
# Create .env file with:
# CAPITALCOM_API_KEY=your_api_key
# CAPITALCOM_API_PASSWORD=your_api_password
# CAPITALCOM_USERNAME=your_username  # Optional: your Capital.com login username
# CAPITALCOM_ENVIRONMENT=demo  # or 'live' for production
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
├── data/            # Capital.com feed, CSV loader, resampler, DTOs
│   ├── dto.py       # Data Transfer Objects (normalized structures)
│   ├── dto_transformers.py  # API response transformers
│   ├── capitalcom_client.py  # REST API client
│   ├── capitalcom_websocket.py  # WebSocket client
│   └── instrument_mapper.py  # Instrument name mapping
├── indicators/      # Supertrend, StochRSI, FVG, Volume Profile
├── strategy/        # Signal generation
├── execution/       # Capital.com broker, backtesting
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
- **smartmoneyconcepts**: FVG detection (optional, fallback implementation included)
- **requests**: HTTP client for Capital.com REST API
- **websocket-client**: WebSocket client for real-time market data
- **loguru**: Logging

## Capital.com API Setup

1. **Generate API Key**:
   - Log in to your Capital.com account
   - Navigate to **Settings** > **API integrations**
   - Click **Generate API key**
   - Enable Two-Factor Authentication (2FA) if not already enabled
   - Provide a name, set a password, and optionally set expiration date
   - Enter your 2FA code to finalize
   - **Save the API key and password securely** (shown only once)

2. **Configure Environment Variables**:
   ```bash
   # .env file (copy from .env.example)
   CAPITALCOM_API_KEY=your_api_key_here
   CAPITALCOM_API_PASSWORD=your_api_password_here
   CAPITALCOM_USERNAME=your_capital_com_username_or_email  # REQUIRED: Your login username/email
   CAPITALCOM_ENVIRONMENT=demo  # Use 'demo' for paper trading, 'live' for production
   ```
   
   **Important**: `CAPITALCOM_USERNAME` is **required** and must be your Capital.com account login username or email address. The API key alone will not work for authentication.

3. **Instrument Format**:
   - The strategy uses standard format (e.g., `EUR_USD`)
   - Capital.com uses "epics" (e.g., `CS.D.EURUSD.CFD.IP`)
   - Automatic conversion is handled by the `InstrumentMapper` class

## DTO Architecture

The project uses a **Data Transfer Object (DTO)** normalization layer that:
- Provides source-agnostic data structures (`CandleDTO`, `PriceDTO`, `AccountDTO`, etc.)
- Makes it easy to add new data sources by implementing transformers
- Keeps strategy code independent of API-specific formats
- Enables easy testing with mock DTOs

To add a new broker/data source, implement `BaseDTOTransformer` and create corresponding client classes.

## Disclaimer

This software is for educational purposes only. Trading forex involves significant risk. Always test with paper trading first.
