# Session-hedging

Standalone FastAPI service for the **session-open hedge**: once per Tokyo / London / New York cash session, after the first completed 15-minute bar, both a long and a short are simulated at the **next bar’s open**. Stop is `2 ×` that first bar’s range (wicks included); take-profit is 1:3. When one side is stopped, the survivor’s stop moves to entry + 20 pips, or to entry (breakeven) if the original stop is smaller than 20 pips.

v1 is **backtest + paper**. It does not place orders. Clients talk only to this process; it pulls closed M15 bars from [ctrader-markets](../ctrader-markets/README.md).

Paper and backtest share the same closed-bar engine. A paper fill is the next **closed** bar’s open — the same as a backtest fill, which is about 15 minutes after a live open in wall-clock time. This is not tick-level execution.

## Layout

`src/` is the package root (no nested `session_hedging/` folder), same pattern as `ctrader-markets` and `ipda`. Default HTTP port is **8012**. The backtest UI lives in `client/`.

## Setup

```bash
cd session-hedging
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
# set CTRADER_API_KEY to the running gateway's API_KEY (ctrader-markets/.env.production on :8010)
```

## Seed a local cache

Offline backtests need a handful of bars on disk. Fetch them from the gateway:

```bash
session-hedging --seed --symbol XAUUSD --timeframe M15 --count 2000
```

Writes `data/candles/XAUUSD/M15.jsonl` (gitignored). Pytest uses the committed fixture under `tests/fixtures/`.

## Run

```bash
session-hedging --validate-config
session-hedging
```

`PAPER_ENABLED=true` (default) polls closed M15 bars every 15 seconds. On first start it **warms** to the latest bar and does not backfill historical session entries. State is `logs/paper_state.json`.

## Backtest UI

With the service running, start the Vite client:

```bash
cd client
npm install
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The dev server proxies `/v1` and `/health` to port 8012. If this service has `API_KEY` set, put the same value in `client/.env` as `VITE_API_KEY`.

To serve the UI from the API process itself:

```bash
cd client && npm run build
```

Then reload [http://127.0.0.1:8012](http://127.0.0.1:8012) (`client/dist` is mounted last, so `/v1`, `/health`, and `/docs` still win).

## Endpoints

| Method | Path | Role |
|---|---|---|
| GET | `/health/live` | Process up |
| GET | `/health/ready` | 200 when ctrader-markets `/health/ready` is 200 |
| GET | `/v1/config` | Form defaults (symbol, sessions, risk). No secrets |
| GET | `/v1/candles` | Local file or gateway proxy (`source=local\|ctrader`) |
| POST | `/v1/backtests` | Run the engine; `source` defaults to local if the cache exists |
| GET | `/v1/paper` | Open pairs, last bar, recent events |

`POST /v1/backtests` accepts optional `lock_pips`, `stop_mode`, `sl_mult`, `fixed_stop_pips`, `rr`, `min_stop_pips`, `qty`, `sessions`, and `performance_unit` so you can retune without restarting.

## Stop sizing

`STOP_MODE` chooses how the stop distance `S` (one R) is measured:

| Mode | `S` | Notes |
|---|---|---|
| `bar_range` (default) | `SL_MULT × opening range over ORB_MINUTES` | `S` varies per session, so R differs per pair |
| `fixed_pips` | `FIXED_STOP_PIPS × PIP_SIZE` | `S` is constant, so R is comparable across sessions |

`FIXED_STOP_PIPS` is required when `STOP_MODE=fixed_pips`; a run configured without it is rejected rather than silently opening no pairs. `MIN_STOP_PIPS` still applies as a floor in both modes. Because the two modes measure R differently, results are only comparable within one mode — the report and `/v1/config` both state `stop_mode` and `fixed_stop_pips`.

## Performance units and grouped results

Backtests report pips by default. A leg's pip result is its signed price movement divided by `PIP_SIZE`; it does not scale with `QTY`. Set `DOLLARS_PER_PIP_PER_QTY` to enable dollar results, calculated as:

```text
dollars = pips × DOLLARS_PER_PIP_PER_QTY × QTY
```

Set `PERFORMANCE_UNIT=dollars` to make dollars the UI default. That default requires a configured dollar-per-pip rate. The UI can switch between available units without rerunning because the API always returns explicit pip fields and returns dollar fields when conversion is configured.

Maximum drawdown is peak-to-trough performance measured after every closed candle. Closed legs use their realized fills and surviving legs are marked at the candle close; intrabar excursions are not estimated.

Each primary and hedge leg also records maximum adverse excursion (`mae`, non-positive) and maximum favorable excursion (`mfe`, non-negative) from entry through its exit bar. These use each closed candle's full high/low because the data does not reveal intrabar ordering. Pip values are always returned; dollar values follow the optional conversion above and are included in CSV downloads when available.

`trade_pairs` is the grouped result contract used by the UI. Each session entry contains a primary leg matching the first candle's direction and the opposite hedge leg, including open/closed status and separate results. The legacy generic P&L fields and flat `trades` list remain available for existing clients and saved paper state.

If `API_KEY` is set, send `X-API-Key`. Leave it empty for local use.

## Session clock

Windows are IANA / cash-session, not TradingView chart timezone:

- Tokyo `Asia/Tokyo:09:00-18:00`
- London `Europe/London:08:00-16:30`
- New York `America/New_York:08:00-17:00`

Gateway candles are stamped at the **end** of the UTC interval. Session membership uses **bar open** (`ts − 15m`) so the New York 07:45–08:00 bar is not treated as the 08:00 open.

Default gold pip size is `0.1`.

## Tests

```bash
.venv/bin/pytest
cd client && npm test
```
