# Forex multi-timeframe strategy (4H Supertrend + 1H StochRSI + FVG + Volume Profile)

This project implements the strategy described in the prompt:

- **Trend filter (higher timeframe)**: 4H Supertrend direction (computed on 4H candles and **shifted by 1 candle** to prevent look-ahead bias).
- **Momentum (entry timing)**: 1H Stochastic RSI cross from oversold/overbought.
- **Price action**: Fair Value Gap detection (ICT-style) via `smartmoneyconcepts`.
- **Context zones**: lightweight volume profile (tick-volume proxy) to identify POC/HVN areas.

It includes:

- A **signal generator** that produces `buy_signal` / `sell_signal` columns.
- A **Backtrader** backtest runner that trades those signals (with spread modeling).
- An **optional OANDA execution client** (not enabled by default).

## Install

From this folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Data format

Provide a CSV of **1H** candles with columns:

- `time` (ISO datetime or epoch; will be parsed)
- `open`, `high`, `low`, `close`
- `volume` (tick volume is fine; if missing, a constant volume will be used)

Example path:

- `data/EURUSD_H1.csv`

## Run a backtest

```bash
python -m forex_mtf_strategy.backtest.run \
  --csv data/EURUSD_H1.csv \
  --symbol EURUSD \
  --cash 10000 \
  --spread-pips 1.3
```

The runner will:

1. Load 1H candles
2. Resample to 4H
3. Compute indicators and signals
4. Run Backtrader using the generated signals

## Notes / assumptions

- **Look-ahead prevention**: the 4H Supertrend direction is always **shifted by 1** before being forward-filled into 1H.
- **Forex sizing**: the included position sizing is intentionally conservative and simplified (account currency assumed equal to quote currency; adapt for production).
- **FVG bounce**: implemented as “price enters the zone and closes back outside” (bullish: closes above the zone; bearish: closes below the zone).

## Project layout

```
trading-algos/forex_mtf_strategy/
  src/forex_mtf_strategy/
    data/
    indicators/
    strategy/
    backtest/
    execution/
```

