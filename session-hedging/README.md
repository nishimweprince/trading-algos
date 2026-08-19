# Session-hedging

Standalone FastAPI service for the **session-open hedge**: once per Tokyo / London / New York cash session, after the first completed 15-minute bar, both a long and a short are simulated at the **next bar’s open**. Stop is `2 ×` that first bar’s range (wicks included); take-profit is 1:3. When one side is stopped, the survivor’s stop moves to entry + 20 pips, or to entry (breakeven) if the original stop is smaller than 20 pips.

v1 is **backtest + paper**. It does not place orders. Clients talk only to this process; it pulls closed M15 bars from [ctrader-markets](../ctrader-markets/README.md).

Paper and backtest share the same closed-bar engine. A paper fill is the next **closed** bar’s open — the same as a backtest fill, which is about 15 minutes after a live open in wall-clock time. This is not tick-level execution.

## Layout

`src/` is the package root (no nested `session_hedging/` folder), same pattern as `ctrader-markets` and `ipda`. Default HTTP port is **8012**.

## Setup

```bash
cd session-hedging
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
# set CTRADER_API_KEY to the gateway key
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

## Endpoints

| Method | Path | Role |
|---|---|---|
| GET | `/health/live` | Process up |
| GET | `/health/ready` | 200 when ctrader-markets `/health/ready` is 200 |
| GET | `/v1/candles` | Local file or gateway proxy (`source=local\|ctrader`) |
| POST | `/v1/backtests` | Run the engine; `source` defaults to local if the cache exists |
| GET | `/v1/paper` | Open pairs, last bar, recent events |

`POST /v1/backtests` accepts optional `lock_pips`, `sl_mult`, `rr`, `min_stop_pips`, `qty`, and `sessions` so you can retune without restarting.

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
```
