# Forex Multi-Timeframe Strategy

This project implements a sophisticated forex trading system combining:
- **4-hour Supertrend** for trend filtering
- **Stochastic RSI** for momentum detection
- **Fair Value Gap (FVG)** price action analysis
- **Volume Profile** zone identification

## Structure

- `config/`: Configuration settings and instruments.
- `data/`: Data fetching and generation.
- `indicators/`: Custom indicator implementations (Supertrend, StochRSI, FVG, Volume Profile).
- `strategy/`: Core strategy logic (`SignalGenerator`).
- `execution/`: OANDA broker integration.
- `risk/`: Position sizing and risk management.
- `backtest.py`: Backtrader implementation for backtesting.
- `main.py`: Entry point CLI.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Backtest
Run the backtest using Backtrader:

```bash
python3 main.py backtest
```

### Signal Generation (Manual/Analysis)
You can use the `SignalGenerator` class in `strategy/signal_generator.py` to analyze DataFrames directly.

```python
from strategy.signal_generator import SignalGenerator
# ... load df_1h and df_4h ...
generator = SignalGenerator(df_1h, df_4h)
signals = generator.generate_signals()
```

## Configuration
Edit `config/settings.py` to adjust parameters like Supertrend length, StochRSI thresholds, etc.
