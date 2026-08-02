# Replay & Labeller Tool — Claude Code Build Handoff

## 0. Purpose (read first)

Build a local, single-user tool for **manual candle replay and discretionary trade labelling**. The operator replays historical candles one bar at a time, identifies a setup, pauses, marks entry/stop/target, records notes and context, then resumes to watch the outcome. Each labelled trade is scored by a deterministic labeler and stored as an **occurrence**. Later, live-identified signals are compared against these stored occurrences to produce a probability.

This tool writes into the same `occurrences` store the automated pipeline will use. A manual row and an automated row have the same shape; they differ only by a `source` column. Keep that invariant.

Phase-1 scope is **manual labelling + the data layer only**. Automated pattern detection, Claude context tagging, live feeds, and ML modelling are explicitly out of scope (see Section 9).

---

## 1. Stack

**Backend:** Python 3.11+, FastAPI, Pydantic v2, uvicorn. Data via DuckDB over partitioned Parquet. `pandas` + `numpy` for the labeler and context computation. `pyarrow` for Parquet.

**Frontend:** React + Vite + TypeScript. shadcn/ui for all UI primitives. TradingView **Lightweight Charts** (MIT) for the candle chart and replay. TanStack Query for server state. `react-hook-form` + `zod` for the trade form. Zustand (or plain context) for replay cursor state.

**Reuse mandate:** build a small set of shadcn-based components and compose them. Do not hand-roll inputs, dialogs, selects, tables, or buttons. Every form control is a shadcn component wrapped once in a typed helper.

---

## 2. Core invariants (do not violate)

1. **No future bars during replay.** The chart only ever renders candles up to the current cursor index. Future candles are not in the series until the cursor reaches them. This is what keeps discretionary labelling honest.
2. **The labeler owns the canonical result.** The operator's eyeballed outcome is never the stored result. Entry/stop/target/side go through `label_triple_barrier` over the forward candles; its return is the canonical `result` and `realized_r`. The operator's subjective read may be stored separately in `observed_result`.
3. **Controlled setup vocabulary.** Every trade is tagged with a `setup_id` from a fixed `setups` list, the same vocabulary the future live detector will emit. Free-text notes are for human review only and never drive comparison.
4. **Manual and auto stay separable.** `source` is on every row. The comparison query defaults to comparing within the same source. Never silently pool discretionary and mechanical trades into one win rate.
5. **UTC everywhere.** Candle timestamps are stored and reasoned about in UTC. Session is derived from UTC hour. Display timezone is a frontend concern only.

---

## 3. Architecture

```
React (Vite + shadcn + Lightweight Charts)
  - loads a candle window once per session
  - reveals candles up to a cursor (play / pause / step / scrub)
  - operator marks entry/stop/target + setup + notes, submits
        |  HTTP (JSON)
        v
FastAPI
  - serves candle windows
  - on trade submit: computes context at signal bar, runs labeler over
    forward candles, writes an occurrence (source='manual'), returns result
  - serves the compare endpoint (win rate / Wilson CI / expectancy)
        |
        v
DuckDB  (engine.duckdb: occurrences, setups, labeling_sessions)
  +  Parquet  (candles/, partitioned, read as a DuckDB view)
```

The backend holds full history, so when a trade is submitted mid-replay the labeler resolves it immediately from stored data. The frontend still lets the operator replay forward to watch it play out; that is for learning, not for scoring.

---

## 4. Repository skeleton

```
trading-algos/
  replay-labeler/
    backend/
      pyproject.toml
      app/
        main.py                 # FastAPI app, CORS, router registration
        config.py               # Settings: data paths, labeler defaults
        db/
          duck.py               # connection factory + view/table bootstrap
          schema.sql            # DDL (Section 6)
          bootstrap.py          # runs schema.sql, registers parquet view, seeds setups
        models/                 # Pydantic schemas
          candle.py
          trade.py              # TradeSubmit (in), Occurrence (out)
          session.py
          compare.py
        services/
          candles.py            # window queries against the parquet view
          context.py            # trend_state, atr_bucket, session, rsi_band (causal)
          labeler.py            # label_triple_barrier (Section 5)
          occurrences.py        # write + query occurrences
          compare.py            # sample-size ladder + Wilson interval
        routers/
          candles.py            # GET /candles, /symbols, /timeframes
          setups.py             # GET /setups
          sessions.py           # POST/PATCH /sessions
          trades.py             # POST /trades, GET /trades
          compare.py            # POST /compare
      tests/
        test_labeler.py         # unit tests incl. intrabar-ambiguity cases
    frontend/
      index.html
      vite.config.ts
      src/
        main.tsx
        lib/
          api.ts                # typed fetch wrapper
          format.ts
        types/
          index.ts              # shared TS types mirroring Pydantic models
        hooks/
          useCandles.ts
          useReplay.ts          # cursor, play state, speed, step()/play()/pause()
          useTrades.ts
          useSetups.ts
          useCompare.ts
        components/
          chart/
            ReplayChart.tsx     # Lightweight Charts wrapper, reveals up to cursor
            PriceLines.tsx      # entry/stop/target overlays
          controls/
            PlaybackControls.tsx  # play/pause/step/scrubber/speed (shadcn Button, Slider)
          trade/
            TradeForm.tsx       # shadcn Form + Select(setup) + Input + Textarea
            TradeList.tsx       # shadcn Table + Badge(result/source)
            ResultBadge.tsx
          session/
            SessionBar.tsx      # symbol/timeframe/date-range picker, start/end session
          common/
            StatCard.tsx        # shadcn Card wrapper (reused for compare stats)
          ui/                   # shadcn generated primitives
        pages/
          ReplayPage.tsx        # main layout: chart + controls + form + list + stats
    data/
      candles/                  # parquet, partitioned (Section 6.1)
      engine.duckdb             # created by bootstrap
    README.md                   # run instructions
```

---

## 5. The labeler (backend/app/services/labeler.py)

Reference implementation. Keep the ambiguity handling and the next-open entry.

```python
import pandas as pd

def label_triple_barrier(
    candles: pd.DataFrame,     # open/high/low/close, ascending, integer position index
    signal_idx: int,
    side: int,                 # +1 long, -1 short
    entry: float,              # operator-marked entry (manual) OR next-open (auto)
    sl: float,                 # operator-marked stop
    tp: float,                 # operator-marked target
    max_bars: int,
    ambiguous: str = "conservative",   # conservative | drop | optimistic
) -> dict:
    highs = candles["high"].to_numpy()
    lows  = candles["low"].to_numpy()
    end = min(signal_idx + 1 + max_bars, len(candles))
    for i in range(signal_idx + 1, end):
        hit_tp = highs[i] >= tp if side == 1 else lows[i] <= tp
        hit_sl = lows[i]  <= sl if side == 1 else highs[i] >= sl
        if hit_tp and hit_sl:
            result = {"conservative": "loss", "drop": "ambiguous", "optimistic": "win"}[ambiguous]
            return _out(result, i, signal_idx, entry, sl, tp, side)
        if hit_tp:
            return _out("win", i, signal_idx, entry, sl, tp, side)
        if hit_sl:
            return _out("loss", i, signal_idx, entry, sl, tp, side)
    return _out("timeout", end - 1, signal_idx, entry, sl, tp, side)

def _out(result, exit_idx, signal_idx, entry, sl, tp, side):
    risk = abs(entry - sl) or 1e-9
    if result == "win":
        realized_r = (tp - entry) / risk * side
    elif result == "loss":
        realized_r = (sl - entry) / risk * side
    else:
        realized_r = None   # timeout: optionally mark-to-close at exit_idx
    return {"result": result, "exit_idx": int(exit_idx),
            "bars_to_resolution": int(exit_idx - signal_idx), "realized_r": realized_r}
```

Note: for **manual** trades the operator marks entry, so pass that marked price. For future **auto** trades, entry is the next bar's open. This is an accepted difference between the two sources and another reason to keep them separable.

---

## 6. Data layer skeleton (phase 1)

### 6.1 Candles (Parquet, read as a DuckDB view)

Partition on disk:

```
data/candles/symbol=EURUSD/timeframe=H1/year=2024/part-000.parquet
```

Columns (all rows UTC, one row per closed bar):

| column | type | notes |
|---|---|---|
| ts | TIMESTAMP | bar close time, UTC, unique per (symbol, timeframe) |
| open | DOUBLE | |
| high | DOUBLE | |
| low | DOUBLE | |
| close | DOUBLE | |
| volume | DOUBLE | may be tick volume for forex |

Registered in DuckDB as a view:

```sql
CREATE OR REPLACE VIEW candles AS
SELECT symbol, timeframe, ts, open, high, low, close, volume
FROM read_parquet('data/candles/**/*.parquet', hive_partitioning = 1);
```

Ingestion is a separate one-time script (`scripts/ingest.py`, stub only in phase 1): read a source CSV/feed, validate ascending UTC timestamps, dedupe on (symbol, timeframe, ts), assert no gaps, write partitioned Parquet.

### 6.2 schema.sql (DuckDB tables)

```sql
CREATE TABLE IF NOT EXISTS setups (
  setup_id      VARCHAR PRIMARY KEY,
  name          VARCHAR NOT NULL,
  description   VARCHAR,
  default_side  INTEGER,             -- +1, -1, or NULL if either
  active        BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS labeling_sessions (
  session_id  UUID DEFAULT uuid() PRIMARY KEY,
  started_at  TIMESTAMP DEFAULT now(),
  ended_at    TIMESTAMP,
  symbol      VARCHAR,
  timeframe   VARCHAR,
  date_from   TIMESTAMP,
  date_to     TIMESTAMP,
  blinded     BOOLEAN DEFAULT FALSE,
  notes       VARCHAR
);

CREATE TABLE IF NOT EXISTS occurrences (
  id                  UUID DEFAULT uuid() PRIMARY KEY,
  source              VARCHAR NOT NULL,        -- 'manual' | 'auto'
  session_id          UUID,
  symbol              VARCHAR NOT NULL,
  timeframe           VARCHAR NOT NULL,
  ts                  TIMESTAMP NOT NULL,      -- signal bar close time
  setup_id            VARCHAR NOT NULL,        -- FK -> setups.setup_id
  side                INTEGER NOT NULL,        -- +1 / -1
  -- marked levels
  entry               DOUBLE, sl DOUBLE, tp DOUBLE,
  -- labeler params + result (params stored so a later rule change is detectable)
  max_bars            INTEGER,
  atr_period          INTEGER,
  atr_at_signal       DOUBLE,
  result              VARCHAR,                 -- win|loss|timeout|ambiguous
  realized_r          DOUBLE,
  bars_to_resolution  INTEGER,
  observed_result     VARCHAR,                 -- optional human read
  -- context (computed causally at signal bar)
  trend_state         VARCHAR,                 -- up|down
  atr_bucket          VARCHAR,                 -- low|mid|high
  session             VARCHAR,                 -- asian|london|ny
  rsi_band            VARCHAR,                 -- oversold|neutral|overbought
  -- optional calendar
  calendar_flag       BOOLEAN,                 -- high-impact news that day
  calendar_tags       VARCHAR,                 -- short comma list, optional
  -- freeform + provenance
  notes               VARCHAR,
  labeler_version     VARCHAR,
  created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_occ_lookup
  ON occurrences (setup_id, symbol, timeframe, trend_state, session);
```

### 6.3 Context definitions (services/context.py, all causal)

Compute over candles up to and including the signal bar only.

- **trend_state:** `up` if signal close > EMA(200) of close, else `down`. (200-period default, configurable.)
- **atr_bucket:** ATR(14) as a fraction of price; bucket against global historical terciles for that symbol+timeframe into `low|mid|high`.
- **session:** from UTC hour of `ts`. Default bands: Asian 00:00–07:00, London 07:00–13:00, NY 13:00–21:00. Configurable.
- **rsi_band:** RSI(14) at signal: `<30 oversold`, `30–70 neutral`, `>70 overbought`.

These are defaults; expose thresholds in `config.py`.

---

## 7. API surface

| method | path | body / params | returns |
|---|---|---|---|
| GET | /symbols | — | list of symbols present in candles |
| GET | /timeframes | symbol | list of timeframes for that symbol |
| GET | /candles | symbol, timeframe, date_from, date_to | full ordered candle window for the session |
| GET | /setups | — | active setups vocabulary |
| POST | /sessions | symbol, timeframe, date_from, date_to, blinded | session_id |
| PATCH | /sessions/{id} | ended_at, notes | ok |
| POST | /trades | session_id, symbol, timeframe, signal_ts, setup_id, side, entry, sl, tp, notes?, calendar_flag?, calendar_tags?, observed_result? | occurrence incl. computed context + labeler result |
| GET | /trades | session_id | occurrences for the session |
| POST | /compare | setup_id, symbol, timeframe, context{trend_state, session, atr_bucket?, rsi_band?}, source? (default 'manual'), min_samples | matched_count, wins, decided, win_rate, wilson_low, wilson_high, expectancy_r, level_used |

**/trades server flow:** locate `signal_idx` for `signal_ts` in the candle series, compute context at that bar, run `label_triple_barrier` over forward candles, assemble the occurrence with `source='manual'`, insert, return.

**/compare sample-size ladder:** try the full context filter; if `decided < min_samples`, relax one dimension at a time (drop rsi_band, then atr_bucket, then session, then trend_state) until the threshold is met; return `level_used` describing which filter produced the numbers, or `no_signal` if even setup-only is too thin. Win rate is `wins / decided` (timeouts excluded from the ratio, reported separately). Wrap in a Wilson interval.

---

## 8. Frontend behaviour

- **Session start:** SessionBar picks symbol, timeframe, date range, optional blinded toggle. On start, POST /sessions and GET /candles for the window.
- **Replay:** ReplayChart renders `candles.slice(0, cursor+1)`. PlaybackControls drive `useReplay`: play (auto-advance at selectable speed), pause, step forward/back, scrubber. When blinded, hide the time axis labels and the calendar date.
- **Marking a trade:** on pause, operator sets entry/stop/target (draggable price lines) and a long/short toggle (side may also be inferred from tp vs entry). TradeForm collects setup (shadcn Select from /setups), notes (Textarea), optional calendar_flag/tags. Submit posts to /trades with `signal_ts` = the cursor bar's ts.
- **After submit:** the returned occurrence (with result) is added to TradeList (shadcn Table, ResultBadge shows win/loss/timeout and source). Operator may resume replay to watch it resolve visually.
- **Live compare (preview of phase 2 use):** a panel where the operator enters a hypothetical current setup + context and calls /compare, rendering win rate, Wilson interval, expectancy, and sample size via reused StatCard components.

Reusable components to build once and reuse: `StatCard`, `ResultBadge`, a typed `FormField` wrapper, `PriceLines`, `PlaybackControls`. Everything else composes shadcn primitives directly.

---

## 9. Out of scope for phase 1 (do not build)

Automated pattern detection, Claude/LLM context tagging, live broker feed or polling, websocket streaming, agent orchestration, LightGBM or any ML/calibration, authentication, multi-user, cloud deployment. The schema already anticipates `source='auto'`, so these slot in later without migration.

---

## 10. Acceptance criteria

- Replaying never reveals a candle past the cursor; stepping/scrubbing is exact.
- Submitting a trade writes exactly one occurrence, scored by the labeler, with computed context attached.
- `result` comes from the labeler, not the operator; `observed_result` (if provided) is stored separately.
- Setups come only from the `setups` table; a trade cannot be submitted without a valid `setup_id`.
- /compare returns a win rate with a Wilson interval and reports which filter level produced it, and returns `no_signal` below `min_samples`.
- All timestamps stored in UTC; sessions derived from UTC hour.
- shadcn primitives are reused, not re-implemented; the labeler has unit tests including intrabar-ambiguity cases.

---

## 11. Open decisions (owner: you — resolve before or during build)

1. **Historical candle source.** The tool needs data to replay. Provide the source (broker export, Dukascopy, HistData, CSV) and the ingestion will target it. Nothing runs without this.
2. **Initial setup vocabulary.** Seed `setups` with your real setup names (this becomes `pattern_id`). Placeholder seed provided below; replace it.
3. **Multi-timeframe context.** Do you want a higher-timeframe reference pane while replaying a lower timeframe, or single-timeframe only for phase 1? Default assumed: single.
4. **Labeler defaults.** Confirm `max_bars`, ATR period, and whether stop/target are fully operator-marked (assumed) or ATR-derived suggestions the operator can accept.
5. **Calendar handling.** Manual `calendar_flag` / tags the operator sets (assumed), or an economic-calendar integration (out of scope suggested).
6. **Blinding.** Include the hide-dates / randomize-window toggle in phase 1, or defer? Default assumed: included as an optional toggle.

Placeholder setups seed (replace in `bootstrap.py`):

```sql
INSERT INTO setups (setup_id, name, default_side) VALUES
  ('bull_engulfing', 'Bullish Engulfing', 1),
  ('bear_engulfing', 'Bearish Engulfing', -1),
  ('pin_bar_long',   'Bullish Pin Bar',   1),
  ('inside_break',   'Inside Bar Break',  NULL);
```
