# Lookup Trader

Local bar replay and manual trade labelling tool for building a pattern-based probability database.

## Architecture

- **`server/`** — FastAPI backend with DuckDB + triple-barrier labeler
- **`client/`** — React + Vite + shadcn/ui + Lightweight Charts replay UI
- **`data/`** — Hive-partitioned Parquet candles + `engine.duckdb`
- **`scripts/`** — HistData CSV ingestion

## Prerequisites

- Python 3.11+
- Node.js 20+

## 1. Ingest HistData candles

Download minute or tick CSV from [HistData](https://www.histdata.com/) and extract to a folder.

HistData ships two ASCII formats:

- **Minute** (`DAT_ASCII_*_M1_*.csv`) — `date,time,open,high,low,close[,volume]`
- **Tick** (`DAT_ASCII_*_T_*.csv`) — `YYYYMMDD HHMMSSmmm,bid,ask,volume` (aggregated to your `--timeframe`)

The ingest script auto-detects the format. Status-report `.txt` files in the same folder are ignored.

```bash
# Dry run
python3 scripts/ingest_histdata.py \
  --input-dir ~/Downloads/histdata/EURUSD \
  --symbol EURUSD \
  --timeframe H1 \
  --dry-run

# Write Parquet
python3 scripts/ingest_histdata.py \
  --input-dir ~/Downloads/histdata/EURUSD \
  --symbol EURUSD \
  --timeframe H1
```

Output: `data/candles/symbol=EURUSD/timeframe=H1/year=YYYY/month=MM/part-000.parquet`

Each calendar month is written to its own partition. Ingesting HistData downloads separately (e.g. one CSV per month) **accumulates** — later months do not overwrite earlier ones. Re-running ingest for the same month merges with the existing file and dedupes on `ts`.

**Recovery:** If you previously ingested multiple months and only the latest month appears in replay, re-ingest each missing month's CSV. Old year-only files (`year=YYYY/part-000.parquet` without `month=`) still work alongside month partitions; you can delete them after confirming the new partitions cover the same range.

### Dev sample data

A small fixture is included for smoke tests:

```bash
mkdir -p /tmp/histdata-sample
cp server/tests/fixtures/sample_histdata.csv /tmp/histdata-sample/
python3 scripts/ingest_histdata.py --input-dir /tmp/histdata-sample --symbol EURUSD --timeframe H1
```

## 2. Bootstrap DuckDB

```bash
cd server
python3 -m app.db.bootstrap
```

Creates `data/engine.duckdb` with `setups`, `labeling_sessions`, `occurrences` tables and registers the candles Parquet view.

To clear labelled trades and start fresh (backs up occurrences first):

```bash
python3 scripts/reset_database.py              # dry run
python3 scripts/reset_database.py --yes        # clear occurrences only
python3 scripts/reset_database.py --full --yes # delete DB and re-bootstrap
```

Stop the dev server first — DuckDB locks the database file while it is running.

## 3. Start everything (recommended)

From the repo root:

```bash
./scripts/dev.sh
```

This boots DuckDB on first run (if needed), then starts:

- **API** — http://localhost:8000
- **Client** — http://localhost:5173 (proxies `/api` to the backend)

Press `Ctrl+C` to stop both.

Optional env overrides: `LOOKUP_SERVER_PORT`, `LOOKUP_CLIENT_PORT`, `LOOKUP_MIN_SAMPLES` (default `30`; lower during dev so `/compare` returns stats before the sample is large).

Client compare panel threshold: `VITE_MIN_SAMPLES` (default `3` in the UI; the server still enforces its own default unless you pass `min_samples` in the request or set `LOOKUP_MIN_SAMPLES`).

## 3a. Start apps separately

### API

```bash
cd server
PYTHONPATH=. python3 -m uvicorn app.main:app --reload --port 8000
```

### Client

```bash
cd client
npm install
npm run dev
```

Open http://localhost:5173 — the Vite dev server proxies `/api` to the backend.

## 4. Workflow

1. **Start session** — pick symbol, timeframe, date range (optional blinded mode)
2. **Replay** — play/pause/step through candles; chart only reveals bars up to the cursor
3. **Mark trade** — set entry/stop/target, choose setup, add notes, submit
4. **Labeler scores** — backend runs triple-barrier over forward candles; `result` is canonical
5. **Compare** — preview win rate with Wilson interval against stored occurrences

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/symbols` | Available symbols |
| GET | `/timeframes?symbol=` | Timeframes for symbol |
| GET | `/candles/bounds?symbol&timeframe` | Available candle range (`min_ts`, `max_ts`, `bar_count`) |
| GET | `/candles?symbol&timeframe&date_from&date_to` | Candle window |
| GET | `/setups` | Controlled setup vocabulary |
| POST | `/sessions` | Start labelling session |
| PATCH | `/sessions/{id}` | End session |
| POST | `/trades` | Submit labelled trade |
| GET | `/trades?session_id=` | List occurrences |
| POST | `/compare` | Win rate with sample-size ladder |

## Tests

```bash
cd server
PYTHONPATH=. python3 -m pytest -q
```

## Acceptance criteria

- [x] Replaying never reveals candles past cursor
- [x] Trade submit writes occurrence scored by labeler with context
- [x] `result` from labeler; `observed_result` optional and separate
- [x] Invalid `setup_id` rejected
- [x] `/compare` returns Wilson interval + `level_used` or `no_signal`
- [x] UTC timestamps throughout
- [x] Labeler unit tests including intrabar ambiguity

## Out of scope (Phase 2+)

Automated pattern detection, Claude tagging, live feeds, ML calibration, auth, multi-user.
