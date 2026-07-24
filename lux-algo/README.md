# lux-algo signal service

Reproduces the **LuxAlgo Supertrend entry** (from `file.txt`, the TradingView Pine v5
indicator) as a standalone Python service, and submits the resulting trades to the
[`mt5-trader`](../mt5-trader) execution service.

## What it does

1. **Polls** a market-data endpoint for **1-minute** OHLC candles (by passing a `quote`
   and a lookback `count`).
2. **Aggregates** those 1M candles into a configurable **target timeframe**
   (`TARGET_TF_MINUTES`, e.g. 3 minutes), treating the still-forming target candle as a
   live bar.
3. **Recomputes** the Supertrend entry on every poll, so a signal can fire **mid-candle**
   (e.g. on minute 2 of a 3-minute candle) — matching how Pine evaluates the realtime bar.
4. **Submits** each entry to mt5-trader (`POST /v1/signals`, `X-API-Key`) as a market
   order with a stop-loss and take-profit.

### The ported signal (from `file.txt` lines 66-83)

```
supertrend(close, sensitivity=5.5, atrLen=11)     # ATR is Wilder/RMA-smoothed
sma9 = ta.sma(close, 13)
bull = ta.crossover(close, supertrend)  and close >= sma9   -> BUY
bear = ta.crossunder(close, supertrend) and close <= sma9   -> SELL
```

- **Stop-loss** = the supertrend line at signal time (the indicator's own trailing stop).
- **Take-profit** = entry ± `RISK_REWARD` × the stop distance.
- Both are individually toggleable (`SEND_STOP_LOSS`, `SEND_TAKE_PROFIT`).

## Confluence: overlay agreement gate

`file.txt` decides the buy/sell purely on the Supertrend, then draws its other overlays
independently. This service keeps the **Supertrend crossover as the trigger** and adds
the overlays as a **directional gate** — each ported faithfully and individually
toggleable (mirroring the script's own input bools):

| Overlay | file.txt | Direction rule | Default |
|---|---|---|---|
| Range Filter | 202–325 | `fdir == 1` (upward) | on |
| SuperIchi | 328–380 | `tenkan > kijun` | on |
| TBO | 382–429 | `fastEMA(20) > mediumEMA(40)` | on |
| Smart Trail | 431–501 | `Trend == 1` | on |
| HA Market Bias | 148–200 | `c2 > o2` | off |
| MACD color | 680–738 | `macd > 0 and hist > 0` | off |
| PSAR | 66, 78 | `psar < ocAvg` | off |

`CONFLUENCE_MODE` controls strictness:

- `unanimous` (default) — every enabled overlay must confirm the trigger direction.
- `threshold` — at least `CONFLUENCE_THRESHOLD` enabled overlays confirm (0 = all).
- `off` — no gating; **reproduces the raw Pine ▲/▼ decision exactly**.

### Counter-trend vetoes (off by default)

The exhaustion/reversal markers are ported and can **veto** an entry (they do not open
trades — mt5-trader executes entries with broker-side SL/TP and has no close-position
endpoint, so these act as entry filters, not active exits):

- `VETO_TP_POINTS` — a major TP Point (`lele`, lines 87–146) against the trade blocks it.
- `VETO_REVERSALS` — an RSI Reversal (lines 503–517) against the trade blocks it.

## Mid-candle policy: fire once, then lock

A signal can appear on any poll while a target candle is still forming and may change
before it closes (repainting). This service emits the **first** signal seen for a bucket
and then **locks** that bucket — at most one entry per target candle. The committed
signal reflects the forming bar at fire time and is never retracted. This is the
deliberate trade-off for mid-candle speed.

The `signal_id` is a deterministic UUIDv5 of (symbol, bucket start, direction), so a
transport retry **replays idempotently** against mt5-trader instead of creating a
duplicate or a `409 idempotency_conflict`.

## Configuration

Copy `.env.example` to `.env` and fill it in. Key variables:

| Variable | Purpose |
|---|---|
| `DATA_API_URL`, `QUOTE`, `DATA_LOOKBACK` | Market-data endpoint, instrument, warmup window |
| `POLL_INTERVAL_SECONDS` | Poll cadence (keep < 60s for mid-candle firing) |
| `TARGET_TF_MINUTES`, `BUCKET_OFFSET_MINUTES` | Strategy timeframe and bucket alignment |
| `SUPERTREND_SENSITIVITY`, `SUPERTREND_ATR_LEN`, `SMA_LEN` | Indicator params (defaults match `file.txt`) |
| `RISK_REWARD`, `SEND_STOP_LOSS`, `SEND_TAKE_PROFIT`, `PRICE_DIGITS` | Exit and rounding |
| `CONFLUENCE_MODE`, `CONFLUENCE_THRESHOLD` | Overlay gate strictness (see Confluence above) |
| `USE_RANGE_FILTER`, `USE_SUPERICHI`, `USE_TBO`, `USE_SMART_TRAIL`, `USE_HA_BIAS`, `USE_MACD_COLOR`, `USE_PSAR` | Enable/disable each overlay filter |
| `VETO_TP_POINTS`, `VETO_REVERSALS` | Counter-trend entry vetoes |
| `MT5_SYMBOL`, `VOLUME`, `DEVIATION_POINTS` | Order fields (symbol must be in mt5-trader's `ALLOWED_SYMBOLS`) |
| `MT5_SIGNAL_API_URL`, `MT5_SIGNAL_API_KEY`, `REQUIRE_READY` | mt5-trader connection |

The data endpoint's response schema is not fixed: `data_client.parse_candles` accepts a
list of objects or arrays and the common field aliases / timestamp encodings. Adjust the
alias tuples in `src/lux_algo/data_client.py` if your feed differs, and set
`DATA_QUOTE_PARAM` / `DATA_COUNT_PARAM` to match its query parameters.

## Run

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # then edit
lux-algo
```

Run **exactly one** instance (like mt5-trader) to avoid duplicate trading decisions.

## Warmup & alignment caveats

- **Warmup:** the Wilder-smoothed ATR needs history to converge to TradingView values.
  Request a generous `DATA_LOOKBACK` (roughly `150 × TARGET_TF_MINUTES` 1M candles);
  signals are suppressed until the series is warmed.
- **Bucket alignment:** buckets align to UTC midnight so 3-minute bars fall on
  :00/:03/:06 like a chart. If your feed's session boundary differs, set
  `BUCKET_OFFSET_MINUTES` and verify against a known TradingView chart.

## One required change in mt5-trader

`source="lux_algo"` was added to `SignalSource` in
`mt5-trader/src/mt5_signal_service/models.py`. It becomes the broker order comment and
reconciliation tag.

## Tests

```bash
pytest
```

Covers the indicator ports (golden values vs Pine semantics), every overlay's direction,
1M→target aggregation including the forming bucket, fire-once-then-lock, the confluence
gate and vetoes, the strategy end-to-end on a synthetic series, the mt5-trader payload
shape, and the data parser.
