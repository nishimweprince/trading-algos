# IPDA signal service

Stores the full TradingView **IPDA_Full** Pine indicator and ports only the **IPDA
buy/sell** Supertrend entry into a Python signal service that submits market orders to
[`mt5-trader`](../mt5-trader).

## What it does

1. **Polls** a market-data endpoint for **1-minute** OHLC candles.
2. **Aggregates** those into a configurable **target timeframe** (`TARGET_TF_MINUTES`).
3. **Recomputes** the IPDA Supertrend entry on every poll (forming-bar aware).
4. **Submits** each entry to mt5-trader (`POST /v1/signals`, `X-API-Key`).

### The ported signal (from `file.txt` Section 5)

```
supertrend(close, sensitivity=5.5, atrLen=11)
sma = ta.sma(close, 13)
BUY  = crossover(close, supertrend)  and close >= sma
SELL = crossunder(close, supertrend) and close <= sma
```

- **Stop-loss** = the supertrend line at signal time (or fixed pip distance when
  `USE_HARD_TARGETS=true`).
- **Take-profit** = entry ± `RISK_REWARD` × stop distance (or fixed pip distance).

No confluence overlays (Range Filter, SuperIchi, TBO, etc.) and no vetoes — IPDA-only.

## Pine Script (TradingView)

`file.txt` is the full combined indicator (`IPDA + MS + OB + FVG + TrendLines`). Paste it
into TradingView → Pine Editor → Add to chart. Alerts for IPDA ▲/▼ can be toggled under
**Alert Settings**.

## Python service

### Install

```bash
cd ipda
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example.forex .env          # or .env.example.deriv -> .env.deriv
# edit DATA_API_KEY / MT5_SIGNAL_API_KEY / QUOTE / MT5_SYMBOL / PIP_SIZE
```

### Run

```bash
ipda                    # loads .env
ipda --profile deriv    # loads .env.deriv
```

### Tests

```bash
pytest
```

## Layout

| Path | Role |
|------|------|
| `file.txt` | Full IPDA Pine v5 indicator |
| `src/` | Signal service modules (installed / imported as `ipda`) |
| `.env.example.forex` / `.env.example.deriv` | Profile templates |
| `symbols.example.*.json` | Multi-instrument manifests |
| `tests/` | Unit tests |

## Notes vs lux-algo

[`lux-algo`](../lux-algo) ports the same Supertrend×SMA trigger **plus** optional overlay
confluence. This project keeps the raw IPDA labels only and uses signal source `ipda`
(accepted by mt5-trader's `SignalSource` enum).
