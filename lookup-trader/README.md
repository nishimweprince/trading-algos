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
- **Tick** (`DAT_ASCII_*_T_*.csv`) — `YYYYMMDD HHMMSSmmm,bid,ask,volume` (bid is aggregated to your `--timeframe`)

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

For the model/live contract, ingest **XAUUSD H1 only** from HistData. Timestamps
are stored as UTC interval ends; H4 is derived automatically from fixed UTC
four-hour buckets. Ingestion also writes per-candle source provenance and
`data/reports/histdata-XAUUSD-H1.json`. Five continuous years are required before
any deployability claim.

Each calendar month is written to its own partition. Ingesting HistData downloads separately (e.g. one CSV per month) **accumulates** — later months do not overwrite earlier ones. Re-running ingest for the same month merges with the existing file and dedupes on `ts`.

**Recovery:** If you previously ingested multiple months and only the latest month appears in replay, re-ingest each missing month's CSV. After month partitions cover the same range, delete legacy year-only files (`year=YYYY/part-000.parquet` without `month=`) to avoid duplicate bars in replay.

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

Creates `data/engine.duckdb` with `setups`, `labeling_sessions`, `occurrences` tables and registers the candles Parquet view. `./scripts/dev.sh` runs bootstrap on startup — you only need this command when the API is **not** running. If you see a lock error, stop the dev server first (`Ctrl+C`).

To clear labelled trades and start fresh (backs up occurrences first):

```bash
./scripts/reset_database.sh              # dry run
./scripts/reset_database.sh --yes        # clear occurrences only
./scripts/reset_database.sh --full --yes # delete DB and re-bootstrap
```

Stop the dev server first — DuckDB locks the database file while it is running.

## 2a. Build the bar feature store

One row per closed candle: causal context, forward outcome, chart shape, and the
pattern tags described below. `/base-rate` and the replay chart read it; without
it they return `no_signal` rather than erroring.

```bash
python3 scripts/build_bar_features.py --symbol XAUUSD --timeframe H1
```

Incremental by default — re-running only rebuilds bars whose forward window has
since elapsed. Pass `--rebuild` to rewrite every bar, `--dry-run` to inspect
without writing.

**If a candle changed**, start the range at least `max(feature_horizons)` bars
*before* it, not at it. A bar's row describes what happened after it, so a
corrected candle invalidates the forward half of every one of the 48 bars leading
up to it — and the row immediately before it also stores that candle's open as
`next_open`. Rebuilding from the changed bar itself leaves those stale:

```bash
python3 scripts/build_bar_features.py --symbol XAUUSD --timeframe H1 --from 2026-03-30 --rebuild
```

**After a `bar_feature_version` bump** (`server/app/config.py`), every runtime
query filters on version equality, so the existing store goes invisible until a
full rebuild finishes. Delete the partition tree first — the writer merges on
timestamp, and rows left from the old version would survive the merge missing the
new columns:

```bash
rm -rf data/features/symbol=XAUUSD/timeframe=H1
```

then rebuild and restart the API. Because the version is read at import time, a
running process serves one version consistently for its lifetime; there is no
half-migrated state as long as the deploy does not restart mid-rebuild.

The store is a build artifact, not operational state — everything in it derives
from `data/candles`, so a changed threshold is a rebuild rather than a migration.

### Outcome-model dataset

Export the fixed v1 label contract (XAUUSD H1, 24 bars, 1.5 ATR target,
1 ATR stop) with one row for each long/short side:

```bash
python3 scripts/export_training_matrix.py --output data/exports/outcome_v1.parquet
```

Only the causal allow-list is exported. Incomplete or unreliable rows are
excluded, and the adjacent `.manifest.json` records source hashes, schema and
label versions, purged chronological folds, and the untouched final holdout.

Train a new immutable candidate only after reviewing the HistData report:

```bash
.venv/bin/python scripts/build_bar_features.py --symbol XAUUSD --timeframe H1 --rebuild
.venv/bin/python scripts/export_training_matrix.py --output data/exports/outcome_v1.parquet
.venv/bin/python scripts/train_outcome_model.py \
  --dataset data/exports/outcome_v1.parquet \
  --version xauusd-h1-outcome-v1-candidate-YYYYMMDD
```

The artifact records the promotion gates and always starts with
`promoted=false`. Keep the terminal holdout frozen while developing candidates.

## Capital.com Demo forward shadow

Capital is used only for post-HistData XAUUSD H1 bid candles. The application has
no order, position, confirmation, or working-order operation. Capital API keys
are trading-capable, so use Demo credentials and keep them only in ignored
`server/.env` (copy `server/.env.example`).

Discover and validate the account's exact spot-gold epic:

```bash
.venv/bin/python scripts/sync_capital.py --check-session
.venv/bin/python scripts/sync_capital.py --search-market Gold
# Set LOOKUP_CAPITAL_EPIC in server/.env to the exact returned XAU/USD epic.
.venv/bin/python scripts/sync_capital.py --check-market
.venv/bin/python scripts/sync_capital.py --dry-run
```

Publish once, then run the no-order worker:

```bash
.venv/bin/python scripts/sync_capital.py
.venv/bin/python scripts/run_shadow_worker.py --once
.venv/bin/python scripts/run_shadow_worker.py
```

The worker polls once per minute, accepts only settled closed H1 candles, derives
H4, rebuilds affected features, writes two idempotent directional predictions to
`data/shadow.sqlite3`, and resolves them after 24 subsequent complete H1 bars.
Run `--once` twice to confirm the second cycle inserts no duplicate. Operational
state is visible at `/health`; reveal-gated records are available from
`/outcome-model/shadow/history`.

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
| GET | `/outcome-model/shadow` | Unpromoted causal candidate inference |
| GET | `/outcome-model/shadow/history` | Reveal-gated forward-shadow ledger |
| GET | `/health` | Candle, Capital, feature, model, parity, and worker health |

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

## Bar pattern tagging

Every closed candle is tagged by deterministic code in `server/app/taggers/`.
Candlestick rules cover `bull_engulfing`, `bear_engulfing`, `pin_bar_long`,
`pin_bar_short`, and `inside_break`. Confirmed-pivot algorithms cover double
tops/bottoms, head-and-shoulders and inverse head-and-shoulders, ascending,
descending and symmetrical triangles, rising/falling wedges, and broadening
formations. Tags are causal: detectors see only bars at or before the anchor.

They are stored on the bar (`bar_tags`, `tag_setup_ids`, `tag_primary_setup_id`,
`tag_count`) and returned by `/context`, which serves them from the store and
falls back to computing them on request when the bar is not in it — an unbuilt
symbol, or history before the store's warmup. `tag_source` says which path ran.

The compare panel shows the tags at the cursor as chips and fills in the setup
when exactly one clears the confidence bar. Blinded sessions show the chips but
never fill the field: the recorded label has to stay the operator's own read.

`confidence` is **match quality** — how good an example of the pattern this bar
is — on a [0.6, 1.0] scale. It is not a probability that the trade works; that is
what `/base-rate` answers.

`/base-rate` can condition on a current complete pattern with
`tag_setup_id=...&tag_state=complete`; forming patterns are excluded by default.
The review script emits both an HTML gallery and a machine-readable JSON verdict
template with aggregate counts.

## Out of scope

LLM/Claude tagging and model distillation have been retired: pattern identity is
kept deterministic and rebuildable. Trading, WebSockets, native Capital H4,
production authentication, and multi-user operation remain out of scope.
