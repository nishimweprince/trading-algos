# lux-algo signal service

Reproduces the **LuxAlgo Supertrend entry** (from `file.txt`, the TradingView Pine v5
indicator) as a standalone Python service, and submits the resulting trades to the
[`mt5-trader`](../mt5-trader) execution service.

## What it does

1. **Polls** a market-data endpoint for **1-minute** OHLC candles (one or more `quote`s
   from `SYMBOLS_FILE`, or a single `QUOTE` in legacy mode).
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

### Hard targets and where they are anchored

With `USE_HARD_TARGETS=true`, SL/TP become fixed distances (`pips × PIP_SIZE`) and are
sent to mt5-trader as `stop_loss_distance` / `take_profit_distance` rather than as
absolute price levels.

This matters because this service evaluates on a **bar close**, and MT5 bars are
bid-based, while a market buy fills at the **ask**. Anchoring the levels here would add
the spread to every long's risk and subtract it from the reward — on XAUUSD at a $0.30
spread, a nominal 25-pip/40-pip pair (1.60 R:R) actually executes as $2.80/$3.70, an R:R
of 1.32. Sells were unaffected, so the distortion was directional. Sending distances lets
mt5-trader measure from the price the order actually fills at, which also removes the
error from price drift during `POLL_INTERVAL_SECONDS`.

Supertrend-mode targets stay absolute: the stop is meant to sit *on the line*, which is a
real price level rather than an offset.

### `PIP_SIZE` must be set explicitly

`PIP_SIZE` is the price movement of one pip. It is deliberately **not** derived from
`PRICE_DIGITS` — that conflates the pip convention with quote precision, and the two are
independent. A broker quoting XAUUSD to 3 decimals instead of 2 would otherwise rescale
every stop by 10× with no config change and no warning.

Gold has no agreed pip convention, so `PIP_SIZE` is calibrated from a chart measurement.
The shipped default comes from this one:

```
XAUUSD short: 4046.607 -> 4042.579 = 4.028 price move = 40.28 pips
  => PIP_SIZE = 4.028 / 40.28 = 0.10
```

On that scale 1 pip is $0.10 of gold, so `STOP_LOSS_PIPS=25` is a $2.50 stop and
`TAKE_PROFIT_PIPS=40` a $4.00 target. To re-calibrate for another instrument or platform,
measure a known move and divide: `PIP_SIZE = price_move / reported_pips`.

Calibrating on a TradingView chart while executing on the MT5 feed is sound. `PIP_SIZE`
is a unit conversion, and both feeds quote XAUUSD in USD per ounce, so "1 pip = $0.10 of
gold" is true on either. Price *levels* are what differ between feeds — the two will not
print the same bid at the same instant — which is exactly the reason hard targets travel
as distances and are resolved against the MT5 fill.

`PRICE_DIGITS`, by contrast, describes the feed you execute on, so read it from
mt5-trader's `GET /v1/market-data/tick` rather than from a TradingView quote.

Signals themselves are computed from mt5-trader's candles, so they will not line up
exactly with a TradingView chart of the same instrument — different feed, different OHLC,
different indicator values. Expect divergence when eyeballing the two side by side.

Leaving `PIP_SIZE` unset falls back to the legacy `10^-(PRICE_DIGITS-1)` derivation for
backward compatibility only.

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

Copy `.env.example.forex` to `.env` (or `.env.forex` with `--profile forex`) and fill it in.
For Deriv, copy `.env.example.deriv` to `.env.deriv` and run with `--profile deriv`.
Key variables:

| Variable | Purpose |
|---|---|
| `DATA_API_URL`, `DATA_LOOKBACK` | Market-data endpoint and warmup window |
| `QUOTE`, `MT5_SYMBOL` | Single instrument (legacy; omit when using `SYMBOLS_FILE`) |
| `SYMBOLS_FILE` | JSON manifest of instruments to research and trade in one process |
| `POLL_INTERVAL_SECONDS` | Poll cadence (keep < 60s for mid-candle firing) |
| `TARGET_TF_MINUTES`, `BUCKET_OFFSET_MINUTES` | Strategy timeframe and bucket alignment |
| `SUPERTREND_SENSITIVITY`, `SUPERTREND_ATR_LEN`, `SMA_LEN` | Indicator params (defaults match `file.txt`) |
| `RISK_REWARD`, `SEND_STOP_LOSS`, `SEND_TAKE_PROFIT`, `PRICE_DIGITS` | Exit and rounding |
| `USE_HARD_TARGETS`, `STOP_LOSS_PIPS`, `TAKE_PROFIT_PIPS`, `PIP_SIZE` | Fixed-distance exits (see above; set `PIP_SIZE` explicitly) |
| `CONFLUENCE_MODE`, `CONFLUENCE_THRESHOLD` | Overlay gate strictness (see Confluence above) |
| `USE_RANGE_FILTER`, `USE_SUPERICHI`, `USE_TBO`, `USE_SMART_TRAIL`, `USE_HA_BIAS`, `USE_MACD_COLOR`, `USE_PSAR` | Enable/disable each overlay filter |
| `VETO_TP_POINTS`, `VETO_REVERSALS` | Counter-trend entry vetoes |
| `MT5_SYMBOL`, `VOLUME`, `DEVIATION_POINTS` | Order defaults (per-instrument overrides in `SYMBOLS_FILE`) |
| `MT5_SIGNAL_API_URL`, `MT5_SIGNAL_API_KEY`, `REQUIRE_READY` | mt5-trader connection |

By default `DATA_API_URL` targets **mt5-trader's `GET /v1/market-data/candles`**, which
returns `{"symbol", "timeframe", "candles": [{"time", "open", "high", "low", "close",
"volume"}]}` (epoch-seconds `time`, integer `volume`) — this is parsed out of the box
(`tests/test_data_client.py::test_parse_mt5_trader_candles_response` pins this contract).
Set `DATA_API_KEY` to the same value as mt5-trader's `API_KEY`, since that endpoint
requires `X-API-Key` auth; `QUOTE` must be one of mt5-trader's `ALLOWED_SYMBOLS`.

The data endpoint's response schema is not otherwise fixed, though: `data_client.parse_candles`
also accepts a list of objects or arrays and the common field aliases / timestamp encodings,
so a different feed can be dropped in. Adjust the alias tuples in
`src/lux_algo/data_client.py` if your feed differs, and set `DATA_QUOTE_PARAM` /
`DATA_COUNT_PARAM` to match its query parameters.

## Multi-instrument profiles

A single lux-algo process can research and trade **multiple quotes** within one profile
(e.g. XAUUSD and BTCUSD on forex, or several volatility indices on deriv). Set
`SYMBOLS_FILE` in your env file to a JSON manifest; when present, `QUOTE` and
`MT5_SYMBOL` are ignored.

Copy the example manifest and edit per-instrument execution fields:

```bash
cp symbols.example.forex.json symbols.forex.json   # XAUUSD + BTCUSD
# or
cp symbols.example.deriv.json symbols.deriv.json   # Vol 75 + Vol 100
```

Add to `.env` or `.env.forex`:

```env
SYMBOLS_FILE=symbols.forex.json
```

Each entry in the manifest:

| Field | Required | Purpose |
|---|---|---|
| `quote` | yes | Market-data instrument (passed to `GET .../candles`) |
| `mt5_symbol` | no | Execution symbol (defaults to `quote`) |
| `pip_size` | no | Overrides profile `PIP_SIZE` for this instrument |
| `price_digits` | no | Overrides profile `PRICE_DIGITS` |
| `volume` | no | Overrides profile `VOLUME` |
| `deviation_points` | no | Overrides profile `DEVIATION_POINTS` |

Strategy and confluence settings (`SUPERTREND_*`, `CONFLUENCE_MODE`, overlay toggles,
etc.) remain **profile-level** and are shared across all instruments. Only execution
and risk calibration fields are per-instrument.

On each poll, lux-algo fetches candles for every instrument in parallel, evaluates each
forming bar independently, and submits **one `POST /v1/signals` per firing instrument**
in the same tick. Signal idempotency is per `(symbol, bucket, direction)` — multiple
simultaneous signals do not conflict.

Without `SYMBOLS_FILE`, behavior is unchanged: set `QUOTE` and `MT5_SYMBOL` for a single
instrument.

## Profiles (multiple instances on one server)

Each profile loads a separate env file from the repository working directory and pairs
with the matching **mt5-trader** instance:

| Command | Env file | Example template | mt5-trader |
|---|---|---|---|
| `lux-algo` | `.env` | `.env.example.forex` | `mt5-signal-service` (:8000) |
| `lux-algo --profile forex` | `.env.forex` | `.env.example.forex` | `mt5-signal-service --profile forex` |
| `lux-algo --profile deriv` | `.env.deriv` | `.env.example.deriv` | `mt5-signal-service --profile deriv` (:8001) |

Process environment variables still override file values. Use profiles to run forex and
Deriv side by side from one clone without cwd tricks.

**Pairing checklist** — for each profile, verify:

| lux-algo variable | Must match |
|---|---|
| `MT5_SIGNAL_API_URL`, `DATA_API_URL` host/port | mt5-trader `PORT` for that profile |
| `MT5_SIGNAL_API_KEY`, `DATA_API_KEY` | mt5-trader `API_KEY` for that profile |
| `MT5_SIGNAL_API_KEY`, `DATA_API_KEY` | mt5-trader `API_KEY` for that profile |
| `MT5_SYMBOL`, `QUOTE` | Single instrument, or every `mt5_symbol` / `quote` in `SYMBOLS_FILE` |

**XAUUSD / gold** on a single-instrument forex setup — edit `QUOTE`, `MT5_SYMBOL`,
`PIP_SIZE`, and `PRICE_DIGITS` in `.env` or `.env.forex` (see comments in
`.env.example.forex`). For gold **and** other majors together, use `SYMBOLS_FILE` instead.

```powershell
# Forex (bash: cp .env.example.forex .env)
Copy-Item .env.example.forex .env
lux-algo

# Deriv (start mt5-signal-service --profile deriv first)
Copy-Item .env.example.deriv .env.deriv
lux-algo --profile deriv
```

Run **exactly one** instance **per profile** to avoid duplicate trading decisions.

## Run

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example.forex .env    # then edit
lux-algo
```

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
