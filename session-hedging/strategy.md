# Session-Open Hedge — Complete Implementation Specification

> **Purpose of this document.** It is a self-contained, exhaustive description of the
> `session-hedging` service as it is actually implemented (not as it is aspirationally
> described). It exists so that a reviewer — human or model — can critique the strategy's
> **edge, risk model, execution assumptions, and measurement validity** without reading the
> source. Every rule below was read out of the code, and the file/line anchors are given so
> claims can be verified.
>
> **How to use it for research.** Section 15 lists the specific questions a reviewer should
> answer, and Section 14 lists behaviours I already suspect are defects or weak assumptions.
> Treat Sections 1–13 as ground truth about *what the code does*; treat Sections 14–15 as the
> agenda for *what should change*.

---

## 0. One-paragraph summary

Once per trading session (Tokyo / London / New York), the engine waits for the **first
completed 15-minute bar whose open falls inside the session window**. It then opens **both a
long and a short** at the **next bar's open** — a symmetric straddle, not a directional bet.
Both legs share one stop distance `S = 2 × (range of that first bar)` and a `1:3` reward
target, so long SL = `entry − S`, long TP = `entry + 3S`, short SL = `entry + S`,
short TP = `entry − 3S`. When price runs far enough in one direction to stop one leg out, the
engine **locks** the surviving leg: its stop is moved to `entry ± 20 pips` if `S ≥ 20 pips`,
otherwise to breakeven at `entry`. From that point the survivor runs to its `3S` target or its
locked stop. The premise is that the loser's `1R` loss is capped, the survivor's risk is
removed or made positive by the lock, and the survivor's `3R` target pays for the pair. There
is no directional filter, no cost model, no time-based exit, and no position sizing beyond a
fixed lot.

---

## 1. Repository and runtime shape

```
session-hedging/
├── src/                     # package root — flat, no nested package dir
│   ├── main.py              # CLI entrypoint: --validate-config, --seed, or serve
│   ├── api.py               # FastAPI app, routes, paper background loop
│   ├── engine.py            # THE STRATEGY. Closed-bar engine shared by backtest + paper
│   ├── models.py            # Pydantic contracts (Candle, EngineParams, reports…)
│   ├── sessions.py          # Session-window parsing and membership
│   ├── candles.py           # ctrader-markets HTTP client + local JSONL cache
│   ├── paper.py             # Paper loop: poll, step engine, persist state, notify
│   ├── notifier.py          # Fire-and-forget notification-service client
│   └── logging_config.py    # JSON console logging
├── client/                  # Vite + React + shadcn backtest UI
├── tests/                   # pytest suite + committed candle fixture
├── data/candles/<SYM>/<TF>.jsonl   # seeded local candle cache (gitignored)
├── logs/paper_state.json    # persisted paper engine state
└── .env / .env.<profile>    # pydantic-settings configuration
```

- Python ≥3.11, FastAPI + uvicorn (single worker), pydantic v2 / pydantic-settings.
- Default port **8012**. Upstream candle gateway `ctrader-markets` on **8010**.
- **The service never places orders.** v1 is backtest + paper only. `paper.py` simulates fills
  against the same engine the backtest uses; the only side effect is a notification POST.

### 1.1 Entry points (`src/main.py`)

| Invocation | Effect |
|---|---|
| `session-hedging --validate-config` | Loads `.env` (or `.env.<profile>`), builds session windows, prints and exits |
| `session-hedging --seed --symbol X --timeframe M15 --count 2000` | Pulls closed bars from the gateway, writes `data/candles/X/M15.jsonl`, exits |
| `session-hedging` | Configures logging, runs uvicorn with `create_app(settings)` |

Configuration errors are reported field-by-field and exit 1. A 401 from the gateway during
`--seed` gets a dedicated "set `CTRADER_API_KEY`" message.

---

## 2. Data contract and the timestamp convention that everything hinges on

`src/models.py::Candle`:

```python
ts: datetime          # tz-aware, REQUIRED — validator rejects naive datetimes
open, high, low, close, volume: float
provider: str = "ctrader"
source_instrument: str
spread: float | None
spread_source: str | None
```

**`ts` is the END of the interval.** A bar labelled `13:15Z` on M15 covers `13:00Z–13:15Z`.
This is inherited from the `ctrader-markets` gateway and it is the single most important
convention in the codebase, because session membership is judged on the bar's **open**:

```python
def bar_open(bar, timeframe_minutes):        # engine.py
    return bar.ts - timedelta(minutes=timeframe_minutes)
```

Without this, the New York `07:45–08:00` bar (stamped `08:00`) would be mistaken for the
session-open bar. `tests/test_engine.py::test_ny_first_bar_uses_open_not_previous_close`
pins this.

`TIMEFRAME_MINUTES` maps `M1…W1` to minutes; `EngineParams.timeframe_minutes` is derived from
the configured (or per-request) timeframe.

Only closed bars are ever consumed. `spread` / `spread_source` are carried on the model but
**never read by the engine** — spread is not part of any fill.

---

## 3. Session windows (`src/sessions.py`)

```python
DEFAULT_SESSION_SPECS = {
    "tokyo":    "Asia/Tokyo:09:00-18:00",
    "london":   "Europe/London:08:00-16:30",
    "new_york": "America/New_York:08:00-17:00",
}
```

- Spec format `TZ:HH:MM-HH:MM`, parsed into a frozen `SessionWindow(name, tz, start, end, weekdays)`.
- `weekdays = {0,1,2,3,4}` — **Mon–Fri, evaluated in the session's local timezone**.
- Membership is **half-open**: `start <= local_time < end`.
- **Windows may not wrap midnight** — `end <= start` is a config error. So a true Sydney or
  a 24h "always" window cannot be expressed.
- Timezones are IANA via `zoneinfo`, so **DST is handled implicitly and correctly** — the
  window tracks the cash session, not a fixed UTC offset. There are DST tests for both
  London and New York.
- **No holiday calendar.** Christmas, Thanksgiving, Golden Week etc. are ordinary sessions.
- `active_session()` exists as a helper (returns `"always"` when there are no windows) but
  the engine does not use it.

Sessions are configured per-deployment with `TRADING_SESSIONS=tokyo,london,new_york` plus
per-session spec overrides, and can be overridden per backtest request.

---

## 4. The engine (`src/engine.py`) — exact mechanics

`ClosedBarEngine` is the whole strategy. One `step(bar)` call per closed bar. Backtest and
paper call the identical code path, which is the design's strongest property: there is no
separate "live" logic that can drift from the tested logic.

### 4.1 State

```python
pairs:            list[Pair]              # every pair ever opened — never pruned
pending:          dict[session -> PendingSignal]   # at most one armed signal per session
prev_in_session:  dict[session -> bool]   # previous bar's membership, for edge detection
stats:            Stats                   # realized, realized_pips, per-side win/be/loss, locks
trades:           list[ClosedLeg]         # flat leg-level closes
events:           list[EngineEvent]       # signal | entry | lock | exit, unbounded
last_bar:         Candle | None
mintick        = pip_size / 10
be_eps         = max(2*mintick, 0.05*pip_size)   # = 0.2 pips for any pip_size
lock_dist      = lock_pips * pip_size
equity_peak_pips, max_drawdown_pips
```

`Pair` holds `id`, `session`, `entry`, `sl_dist`, the four levels, `primary_side`,
`long_open`, `short_open`, `locked`, `entry_ts`. Pair id is `f"{session}:{fill_bar_ts}"`.

### 4.2 Order of operations inside one bar — this matters

```python
def step(bar):
    self._fill_pending(bar)     # 1. fill any signal armed on the PREVIOUS bar, at bar.open
    self._manage_pairs(bar)     # 2. exit logic for ALL open pairs, incl. the one just filled
    self._arm_signals(bar)      # 3. detect a session-open bar and arm a signal for next bar
    self.last_bar = bar
    self._record_equity(bar.close)   # 4. mark-to-close drawdown accounting
```

Consequences:
- A pair filled at `bar.open` **is immediately exposed to that same bar's high/low**. A
  violent session-open bar can stop one or both legs on the fill bar itself.
- A signal armed on bar *N* is always filled at the open of bar *N+1*. There is a
  deterministic one-bar (15 minute) lag between the signal and the position.

### 4.3 Signal arming (`_arm_signals`)

```python
open_ts     = bar.ts - timeframe_minutes
is_doji     = skip_doji and bar.close == bar.open
valid_range = (bar.high - bar.low) > 0

for window in windows:
    in_now = window.contains(open_ts)
    was    = prev_in_session[window.name]
    if in_now and not was and valid_range and not is_doji:
        pending[window.name] = PendingSignal(
            range_price = bar.high - bar.low,     # full range, wicks included
            bullish     = bar.close > bar.open,
            signal_ts   = bar.ts,
        )
        emit EngineEvent(kind="signal", detail={"range", "bullish"})
    prev_in_session[window.name] = in_now
```

- Trigger is the **rising edge** of session membership — the first bar whose *open* is inside
  the window and whose predecessor's open was not. Naturally once per session per day.
- `range_price` is the **full high−low including wicks**, not the body.
- `bullish` is `close > open`. A doji (`close == open`) is skipped entirely when
  `SKIP_DOJI=true`; a zero-range bar is always skipped.
- `pending` is keyed by session, so a second signal for the same session before the fill
  would silently overwrite the first — not reachable in practice given daily edges.
- Sessions are fully independent: three overlapping pairs (Tokyo, London, NY) can be live at
  once, and NY/London overlap in wall-clock time.

### 4.4 Fill (`_fill_pending` → `_open_pair`)

```python
for session, signal in pending.items():
    _open_pair(session, entry=bar.open, range_price=signal.range_price, ts=bar.ts, bullish=…)
    del pending[session]

base      = fixed_stop_pips * pip_size if stop_mode == "fixed_pips" else range_price * sl_mult
sl_dist   = max(base, min_stop_pips * pip_size)
if sl_dist <= 0: return                      # silent skip, no event emitted
long_sl   = entry - sl_dist
long_tp   = entry + sl_dist * rr
short_sl  = entry + sl_dist
short_tp  = entry - sl_dist * rr
primary_side = "long" if bullish else "short"
```

- **Entry price is the next bar's open, for both legs, with no spread and no slippage.**
- `sl_mult=2`, `rr=3` by default; `min_stop_pips` is a floor (default 0, i.e. inactive).
- `STOP_MODE=bar_range` by default. Under `fixed_pips`, `S` is `FIXED_STOP_PIPS × pip_size` and
  the opening range no longer affects the stop, so `R` is constant across sessions.
- **Both legs are always taken regardless of `bullish`.** The signal bar's direction only
  labels which leg is called `primary` and which `hedge` in the reporting layer. It does not
  affect execution, sizing, or exits at all.
- `qty` is identical on both legs. There is no imbalance, no ratio hedging, no scaling.
- An `entry` event records `entry`, `sl_dist`, `sl_pips`, `bullish_signal`, `primary_side`,
  `pair_id`.

### 4.5 Exit management (`_manage_pairs`) — the full decision tree

Per bar, per pair with at least one open leg:

```python
long_hit_sl  = long_open  and bar.low  <= long_sl
long_hit_tp  = long_open  and bar.high >= long_tp
short_hit_sl = short_open and bar.high >= short_sl
short_hit_tp = short_open and bar.low  <= short_tp
```

**Branch A — not locked, both stops touched on the same bar:**
close long at long_sl, close short at short_sl. **No lock is applied.** This is the whipsaw
case: the pair loses `2R` in one bar. `tests/test_engine.py::test_both_stops_same_bar_no_lock`.

**Branch B — not locked, exactly one stop touched:**
1. `long_hit_sl and short_open` → close long at its stop, `_apply_lock(long_survives=False)`,
   then **on the same bar** if `bar.low <= short_tp`, also close the short at TP.
2. `short_hit_sl and long_open` → mirror image.
3. `elif long_hit_tp` → close long at TP.
4. `elif short_hit_tp` → close short at TP.

Note branches 3 and 4 are effectively unreachable whenever `rr ≥ 1`, because
`long_tp = entry + 3S` lies beyond `short_sl = entry + S`, so any bar that reaches the long TP
has already reached the short SL and is consumed by branch 2. They only become live for
`rr < 1` or from restored state.

**Branch C — already locked:** each leg is handled independently, **stop checked before
target**:
```python
if long_open:  long_hit_sl  -> close at long_sl   elif long_hit_tp  -> close at long_tp
if short_open: short_hit_sl -> close at short_sl  elif short_hit_tp -> close at short_tp
```

### 4.6 The lock (`_apply_lock`) — the core idea of the strategy

```python
if sl_dist >= lock_dist and lock_dist > 0:
    new_sl = entry + lock_dist   if long_survives else entry - lock_dist
else:
    new_sl = entry                                     # plain breakeven
survivor.sl = new_sl
pair.locked = True
stats.locks += 1
emit EngineEvent(kind="lock", detail={"long_survives", "new_sl"})
```

Read carefully: with defaults (`lock_pips=20`, `pip_size=0.1` → `lock_dist = 2.0` price
units on gold, i.e. $2.00), a pair whose stop distance `S` is at least 20 pips gets its
survivor's stop moved to **20 pips in profit**; a tighter pair only gets breakeven. `locked`
is a one-way latch — a pair locks at most once, and the lock never trails further.

The intended pair outcomes:

| Path | Loser | Survivor | Pair result (gross, ignoring costs) |
|---|---|---|---|
| One side stops, survivor hits TP | −1R | +3R | **+2R** |
| One side stops, survivor hits lock (S ≥ 20 pips) | −1R | +20 pips | −1R + 20 pips |
| One side stops, survivor hits breakeven lock | −1R | ~0 | **−1R** |
| Both stop on the same bar | −1R | −1R | **−2R** |
| Never resolves | open | open | carried indefinitely |

So the strategy needs the "one side stops, survivor reaches 3R" path often enough to pay for
the `−1R` lock-outs and the `−2R` whipsaws. Because `R` is proportional to the session-open
bar's range, `R` is **not constant across trades** — a wide open bar means a wide stop, and
since `qty` is fixed, wide-range days risk proportionally more money.

### 4.7 Fill price model

```python
def _fill_stop(open_px, level, going_down):
    return open_px if (open_px <= level if going_down else open_px >= level) else level

def _fill_limit(open_px, level, is_long_tp):
    return open_px if (open_px >= level if is_long_tp else open_px <= level) else level
```

- If the bar **opens beyond** the level (a gap), the fill is the **bar open** — realistic
  gap handling, and the only place where the model is pessimistic on stops.
- Otherwise the fill is **exactly at the level**. No slippage, no spread, no partial fills,
  no rejection, no requote.
- `tests/test_engine.py::test_gap_through_stop_fills_at_open` pins the gap case.

### 4.8 Intrabar path ambiguity — what the code assumes

Only OHLC is available; the intrabar path is unknown. The code's implicit assumptions:

- **Locked branch:** stop is checked before target, so a bar containing both is scored as a
  stop-out. Conservative.
- **Branch A (both stops in one bar):** both are booked as losses. Conservative.
- **Branch B:** one leg stops, the lock is applied, and then the survivor's **original TP** is
  checked on the same bar. But the survivor's **newly-locked stop is not re-checked on that
  same bar.** A bar that spikes one way (stopping leg 1), reverses hard through the lock
  level, and then extends to the survivor's TP will be booked as a full TP win — optimistic.
  Symmetrically, a bar that stops leg 1 then reverses past the lock without reaching TP does
  not book the lock exit until the next bar.

This asymmetry is the main measurement risk in the backtest and it lives entirely in
Branch B, which is *the* path the strategy depends on.

### 4.9 Accounting: three parallel P&L units

```python
_pnl(is_long, entry, exit)      = (exit-entry if long else entry-exit) * qty * point_value
_pnl_pips(is_long, entry, exit) = (exit-entry if long else entry-exit) / pip_size    # NOT × qty
_pips_to_dollars(pips)          = pips * dollars_per_pip_per_qty * qty   (None if unconfigured)
```

- `pnl` is a legacy price-delta × quantity number. `point_value` defaults to `1.0` and is
  **not wired to any environment variable**, so it is always 1.
- `pnl_pips` is the primary reporting unit and deliberately does **not** scale with `qty`.
- `pnl_dollars` is only available when `DOLLARS_PER_PIP_PER_QTY` is set. Setting
  `PERFORMANCE_UNIT=dollars` without it is a startup validation error.
- `equity = initial_capital + realized + unrealized` uses the **legacy price-delta** figure,
  i.e. it adds price deltas to a currency balance. `equity_dollars` is the meaningful one and
  is `None` unless conversion is configured.

Phase 1 adds `src/costs.py`. Spread, slippage, and commission are charged in pips on every actual
entry/exit side; swap accrues per open leg at broker-local rollover boundaries, with the configured
triple weekday covering the weekend. Report totals keep gross, cost, and net pips/R adjacent.
Legacy unprefixed pip/R totals remain gross compatibility aliases. Break-even pips per side is
gross weighted expectancy divided by transacted-side equivalents, and the cost headroom ratio
compares it with the configured spread.

**Outcome buckets** (`_bucket`): `win` if signed move `> be_eps`, `loss` if `< -be_eps`, else
`be`, where `be_eps = 0.2 pips` for any pip size. Buckets are computed from the **fill price**,
not the bar close, so a breakeven-lock exit is correctly counted as `be` rather than a loss
(`tests/test_engine.py::test_be_bucket_from_fill_not_bar_close`). Counters are kept per side
(`long_wins/long_be/long_loss`, same for short) plus a `locks` counter — **not per role**, so
"how often did the hedge leg win" is not directly in `Stats`.

### 4.10 Drawdown

```python
def _record_equity(mark):                       # mark = bar.close, once per bar
    equity_pips = realized_pips + unrealized_pips(mark)
    equity_peak_pips  = max(equity_peak_pips, equity_pips)
    max_drawdown_pips = max(max_drawdown_pips, equity_peak_pips - equity_pips)
```

- Peak-to-trough, **in pips**, sampled **once per closed bar**, with open legs marked at that
  bar's close. Intrabar excursions are invisible, so reported drawdown is a lower bound.
- `equity_peak_pips` starts at `0.0`, so the very first losing stretch counts as drawdown from
  zero (correct for a from-flat backtest).
- Reported as paired gross/net drawdown in pips and R; `max_drawdown_pips` and
  `max_drawdown_r` remain gross compatibility aliases. Net peaks persist in paper snapshots. There is no
  drawdown-in-percent, no per-session drawdown, no Sharpe/Sortino/expectancy/profit-factor.

### 4.11 Reporting: grouped `trade_pairs`

`_trade_pair_results(mark)` reconstructs pair-level results by joining closed legs on
`(pair_id, side)`:
- `status`: `open` (2 legs live), `partial` (1), `closed` (0).
- `primary` / `hedge` assigned from `pair.primary_side`; when it is `None` (state restored
  from a pre-`primary_side` snapshot) both legs land in `unknown_legs`.
- Open legs are marked to `mark` (the last bar's close) so an unfinished pair still shows a
  running P&L.
- The flat `trades: list[ClosedLeg]` and legacy `pnl` fields remain for older clients.

### 4.12 Persistence (`snapshot` / `restore`)

`snapshot()` serialises `prev_in_session`, `pending`, `pairs`, `stats`, `trades`. `restore()`
rebuilds them defensively and back-fills `realized_pips` by re-deriving it from `trades` when
loading an older snapshot that lacks the field. `events`, `max_drawdown_pips`, and
`equity_peak_pips` are **not persisted** — drawdown accounting restarts from zero on every
paper restart, and the event log is lost.

---

## 5. Paper trading loop (`src/paper.py`, `api.py::_paper_loop`)

```python
while True:
    try: await trader.tick()
    except Exception: log("paper_tick_failed")
    await asyncio.sleep(POLL_INTERVAL_SECONDS)     # default 15s
```

`tick()`:
1. Fetch the last `PAPER_LOOKBACK` (default 200) **closed** bars from the gateway.
2. **First ever tick** → `engine.observe(last_bar)` sets `prev_in_session` from that bar only,
   sets `last_ts`, saves, and returns. This is the **warm start**: the service deliberately
   does not backfill historical session entries when it boots.
3. Otherwise take `new = [bar for bar in candles if bar.ts > last_ts]`, `step()` each in order,
   advance `last_ts`, log every emitted event, and fire a notification per `entry|lock|exit`.
4. Persist `logs/paper_state.json` after processing.

Properties and gaps:
- Deduplication is purely `ts > last_ts`. **Revised/corrected bars are ignored**, and an
  out-of-order or backfilled bar older than `last_ts` is silently dropped.
- If the process is down longer than `PAPER_LOOKBACK × timeframe` (200 × 15m ≈ 50 hours), the
  missed bars are lost and the engine resumes with a hole — pairs that would have exited
  during the gap are evaluated against the wrong bars.
- There is no reconciliation with any broker, because there is no broker.
- Notifications are best-effort: `Notifier.send` swallows every exception and logs a warning.
- A paper "fill" is the next closed bar's open, i.e. it is recorded **~15 minutes after the
  real-world session-open bar closes**, plus up to one poll interval of latency. This is
  identical to the backtest, which is the point — but it means paper results do **not**
  measure real execution.

---

## 6. Candle sourcing (`src/candles.py`)

- `fetch_ctrader(symbol, tf, count, to)` pages backwards using a `to` cursor, deduplicating by
  `ts` into a dict, stopping when a page returns nothing new or the cursor stops advancing.
  Page size cap `DEFAULT_PAGE_SIZE = 5000`. Returns the newest `count` bars, ascending.
- `fetch_range(date_from, date_to)` estimates the bar count from the wall-clock span
  (`span_minutes / tf_minutes + 8`) — this **over-counts, because it does not exclude weekends
  and market closures**, and then filters by date. Harmless but wasteful; it also means a
  long-range request can hit the paging loop many times.
- `load_local` / `write_local` handle `data/candles/<SYMBOL>/<TF>.jsonl`, one
  `Candle.model_dump_json()` per line, sorted by `ts` on write.
- `gateway_ready()` probes the upstream `/health/ready`.
- **No data quality validation anywhere**: no gap detection, no check for duplicate or
  non-monotonic timestamps, no OHLC sanity check (`low <= open,close <= high`), no verification
  that the returned timeframe matches the requested one, no missing-session detection.

---

## 7. HTTP API (`src/api.py`)

| Method | Path | Behaviour |
|---|---|---|
| GET | `/health/live` | `{"status":"ok"}` |
| GET | `/health/ready` | 200 when the ctrader gateway's `/health/ready` is 200, else 503 |
| GET | `/v1/config` | Symbol, timeframe, sessions, risk params, performance unit. No secrets |
| GET | `/v1/candles` | `symbol`, `timeframe`, `count`, `to`, `source=local\|ctrader` |
| POST | `/v1/backtests` | Runs the engine over a candle set, returns `BacktestReport` |
| GET | `/v1/paper` | `enabled`, `last_ts`, open pairs, `Stats`, last 50 events |

- Auth: optional `X-API-Key` compared with `hmac.compare_digest`; disabled when `API_KEY` is
  empty. Failures log `authentication_failed` with the path.
- Source resolution: an explicit `source` wins; otherwise **local if the cache file exists**,
  else the gateway.
- Per-request overrides on `POST /v1/backtests`: `lock_pips`, `sl_mult`, `rr`,
  `min_stop_pips`, `qty`, `sessions`, `performance_unit`, `date_from`, `date_to`, `symbol`,
  `timeframe`, `source`. Requesting dollar output without a configured conversion rate → 422.
- Each backtest constructs a **fresh** `ClosedBarEngine`; there is no shared mutable state
  between requests and no result caching.
- `bar_count` is stamped onto the report by the route via `model_copy` (the engine sets 0).
- `client/dist` is mounted last at `/` when built, so `/v1`, `/health` and `/docs` still win.
- Not present: CORS config (the Vite dev server proxies instead), rate limiting, request-size
  limits, pagination on the report (a long backtest returns every trade, pair, and event in
  one JSON body).

---

## 8. Configuration surface (`src/config.py`, `.env.example`)

| Variable | Default | Meaning / strategy impact |
|---|---|---|
| `SYMBOL` | `XAUUSD` | Instrument. Tuned for gold |
| `TIMEFRAME` | `M15` | Signal bar and step granularity |
| `PIP_SIZE` | `0.1` | Gold pip. Drives pips, `be_eps`, `lock_dist`, `min_stop_pips` |
| `STOP_MODE` | `bar_range` | `bar_range` \| `fixed_pips` |
| `SL_MULT` | `2` | `bar_range` only: `S = SL_MULT × opening range` |
| `FIXED_STOP_PIPS` | `0` | `fixed_pips` only, and required there: `S = FIXED_STOP_PIPS × PIP_SIZE` |
| `RR` | `3` | TP = `RR × S` |
| `MIN_STOP_PIPS` | `0` | Floor on `S` in both modes; inactive by default |
| `LOCK_PIPS` | `20` | Survivor's stop → entry ± 20 pips when `S ≥ 20 pips`, else breakeven |
| `QTY` | `1` | Fixed size, identical on both legs. No risk-based sizing |
| `SKIP_DOJI` | `true` | Skip the session when the open bar closes exactly at its open |
| `INITIAL_CAPITAL` | `100000` | Only used for the `equity` field |
| `PERFORMANCE_UNIT` | `pips` | UI default unit |
| `DOLLARS_PER_PIP_PER_QTY` | unset | Required for dollar output; `dollars = pips × rate × qty` |
| `COST_MODEL` | `per_session` | `none` parity control or base schedule plus session overrides |
| `SPREAD_PIPS_PER_SIDE` / `SLIPPAGE_PIPS_PER_SIDE` / `COMMISSION_PIPS_PER_SIDE` | `0` | Execution cost charged on each actual transaction side |
| `SWAP_LONG_PIPS_PER_ROLLOVER` / `SWAP_SHORT_PIPS_PER_ROLLOVER` | `0` | Financing per broker rollover crossed |
| `SWAP_ROLLOVER_TIME` / `SWAP_TIMEZONE` / `SWAP_TRIPLE_WEEKDAY` | `17:00` / `America/New_York` / `wednesday` | DST-aware financing calendar |
| `SESSION_COST_OVERRIDES` / `BREAKEVEN_COST_REPORT` | `{}` / `true` | Per-session numeric schedule patches and cost-budget reporting |
| `TRADING_SESSIONS` | `tokyo,london,new_york` | Which windows are armed |
| `SESSION_*` | see §3 | `TZ:HH:MM-HH:MM` overrides |
| `PAPER_ENABLED` | `true` | Runs the background loop |
| `POLL_INTERVAL_SECONDS` | `15` | Gateway poll cadence |
| `PAPER_LOOKBACK` | `200` | Bars fetched per tick — also the max recoverable outage |
| `CTRADER_MARKETS_URL` / `CTRADER_API_KEY` | `:8010` | Upstream gateway; placeholder value rejected |
| `API_KEY` | empty | Optional auth on this service |
| `NOTIFICATIONS_*` | off | Optional notification-service fan-out |

Validation: blank secrets normalise to `None`; the `.env.example` placeholder API key is
rejected outright; notification channels must be in `{TELEGRAM, EMAIL, SMS, WHATSAPP}`;
`PERFORMANCE_UNIT=dollars` requires `DOLLARS_PER_PIP_PER_QTY`; `STOP_MODE=fixed_pips` requires
`FIXED_STOP_PIPS > 0`, both at startup and on a per-request override.

**Not configurable yet:** per-session strategy parameter sets (all sessions share one
`SL_MULT`/`RR`/`LOCK_PIPS`/`QTY`), any exposure cap, or any time-based exit.

---

## 9. Client (`client/`)

Vite + React + TypeScript + Tailwind + shadcn/ui. `RunForm` posts backtests with parameter
overrides; `KpiStrip`, `BacktestChart`, `SessionRail`, and a sortable `TradeBlotter` render the
report. The UI can switch between pips and dollars without re-running because the API always
returns explicit pip fields and returns dollar fields when conversion is configured. Win rate
is computed as `wins / (wins + be + loss)` — **breakeven exits count in the denominator**, which
depresses the headline number relative to the usual convention. The UI's `TIMEFRAMES` list
(`M1,M5,M15,M30,H1,H4,D1`) is narrower than the backend's `Timeframe` enum.

---

## 10. Test coverage (`tests/`)

Engine: interval-start derivation; the NY 07:45 bar not being the signal; fill at next open;
doji skip; both-stops-same-bar with no lock; breakeven lock when `S < lock_dist`; +20 pip lock
when `S ≥ lock_dist`; gap-through-stop filling at the open; BE bucketing from the fill;
three independent sessions each opening a pair; pip/dollar conversion; closed-bar drawdown
marking; bearish signal producing `primary=short`/`hedge=long`; legacy snapshot restore.

Sessions: spec parsing and rejection, London and New York DST, Tokyo, half-open boundaries,
weekend exclusion, unknown-name rejection.

Paper: first tick warms without entering; a new bar opens at most one pair per session;
reload skips duplicates. API: health, local-fixture backtest, risk overrides, dollar-mode 422,
candles, config, paper-when-disabled. Config: placeholder rejection, blank secrets, dollar
validation, conversion reaching the engine.

**Not covered:** multi-day/multi-session sequences with realistic data, the same-bar
lock-then-TP path in Branch B, unreachable Branch B sub-cases, a full statistical validation of
the strategy on out-of-sample data, any cost sensitivity, any parameter sweep.

---

## 11. Complete worked example (defaults, XAUUSD, M15)

1. `13:00–13:15Z` bar closes, stamped `13:15Z`. Its open `13:00Z` = `08:00` New York → rising
   edge of the NY window. Bar is `O 2000.0, H 2010.0, L 2000.0, C 2008.0`.
2. Range = `10.0`; not a doji; bullish. Signal armed: `range=10.0, bullish=True`.
3. Next bar opens at `2009.0`. Pair opens: `entry = 2009.0`, `S = 2 × 10 = 20.0` (= 200 pips),
   `long_sl = 1989.0`, `long_tp = 2069.0`, `short_sl = 2029.0`, `short_tp = 1949.0`,
   `primary_side = "long"`.
4. Price rallies; a later bar's high reaches `2029.0` → short stops out at `2029.0`
   (`−200 pips`). `S = 20.0 ≥ lock_dist = 2.0`, so the long's stop moves to
   `2009.0 + 2.0 = 2011.0` — 20 pips locked in. `locked = True`.
5. Path (a): the long later trades `2069.0` → `+600 pips`. Pair = `+400 pips`.
   Path (b): price reverses to `2011.0` → long closes `+20 pips`. Pair = `−180 pips`.
6. Meanwhile Tokyo's and London's pairs from the same day may still be open and are managed
   independently on every bar.

---

## 12. What the strategy explicitly does NOT do

- No directional filter, trend filter, volatility regime filter, or news filter — every
  qualifying session is traded.
- No cost model whatsoever: **no spread, no commission, no swap/financing, no slippage**.
  Holding both directions overnight would incur double swap in reality, and gold swap is
  typically negative on both sides.
- No time-based exit, no end-of-session flat, no end-of-day flat, no max holding period. A
  pair opened on a Tokyo session can remain open for weeks until an SL or TP is touched.
- No position sizing model. `QTY` is constant while `R` varies with the session-open range, so
  **risk per trade is not constant**.
- No exposure limits: three sessions × unbounded days of unresolved pairs can accumulate
  arbitrarily many simultaneous legs.
- No account model: no margin, no free-margin check, no stop-out, no leverage.
- No broker feasibility model: simultaneous long and short in one account requires a hedging
  account. FIFO/anti-hedging regimes (e.g. US NFA rules) would net the legs and make this
  structure impossible as written.
- No holiday calendar, no half-day handling.
- No walk-forward, parameter sweep, Monte Carlo, or out-of-sample framework.
- No trade-level metrics beyond win/be/loss counts and pip totals: no expectancy, profit
  factor, Sharpe, MAE/MFE, or per-session breakdown in the API (the UI derives some of this).

---

## 13. Known correctness observations (verified against the code)

These are behaviours I believe a reviewer should treat as bugs or near-bugs, listed so the
review can confirm or dismiss them rather than rediscover them.

1. **`self.pairs` is never pruned.** Fully closed pairs stay in the list forever. `step()`
   iterates all of them each bar, `_trade_pair_results` rebuilds all of them, and `snapshot()`
   serialises all of them into `logs/paper_state.json` on every tick. Long-running paper grows
   without bound in memory, state size, and per-bar cost.
2. **`self.events` is unbounded** too; only the API response is truncated to the last 50.
3. **Unlocked pair with one leg already closed can ignore its stop.** In Branch B the guards
   are `long_hit_sl and pair.short_open` / `short_hit_sl and pair.long_open`. If a pair is
   unlocked but only one leg remains open, neither guard fires and control falls to the
   `elif long_hit_tp` / `elif short_hit_tp` arms — so a **stop hit on the surviving leg is not
   processed on that bar**. Reachable via restored state or `rr < 1`.
4. **`primary_side is None` after restoring an old snapshot** leaves the pair permanently
   unclassified; its legs land in `unknown_legs` and the role field on its closed legs is
   `"unknown"`.
5. **Branch B does not re-check the survivor's newly-locked stop on the same bar** but does
   check its take-profit — optimistic on exactly the path the strategy relies on (§4.8).
6. **Backtest boundary artifact:** a fresh engine starts with `prev_in_session` all `False`, so
   if the very first candle in the dataset already falls inside a session window, that bar is
   treated as a session open and arms a spurious signal. Paper avoids this via `observe()`;
   backtests do not. Every date-ranged backtest that starts mid-session gets one fake trade.
7. **`equity` mixes units** — `initial_capital + realized + unrealized` adds price deltas to a
   cash balance (`point_value` is hardcoded at 1 and unreachable from config).
8. **Drawdown state is not persisted**, so paper's `max_drawdown_pips` silently resets on every
   restart. It is also absent from `PaperStatus` entirely.
9. **`sl_dist <= 0` skips the pair silently** — no event, no log, no counter. Only reachable
   via a zero range, which `_arm_signals` already filters, but the silent path is a trap for
   future parameter changes.
10. **`fetch_range` over-estimates bar count** by including weekends and closures in the span.
11. **No candle validation** — a bad bar from the gateway (inverted high/low, duplicate ts,
    wrong timeframe) propagates straight into fills and stops.
12. **Paper outage recovery is capped** at `PAPER_LOOKBACK` bars, and bars older than
    `last_ts` are dropped without warning, so a gap is invisible in the logs.
13. **Win rate counts breakeven exits in the denominator** in the client, which is a
    presentation choice worth stating explicitly since the strategy *manufactures* breakeven
    exits by design.
14. **A single `httpx.AsyncClient` and a single uvicorn worker** serve both the paper loop and
    backtests; a long synchronous `engine.run()` on a big dataset blocks the event loop and
    therefore stalls the paper loop.

---

## 14. Strategy-level assumptions a reviewer should challenge

- **Is a symmetric straddle at the session open a real edge, or a spread-and-swap tax?** With
  no directional filter, the pair's expectancy is entirely determined by the conditional
  distribution of post-session-open excursions. Cost drag is paid on *every* pair, twice.
- **Is `S = 2 × first-bar range` a sensible volatility proxy?** It is a one-bar estimate with
  no smoothing — an outlier open bar produces a huge stop and a huge target that may be
  unreachable within any realistic horizon; a compressed open bar produces a stop that is
  inside the spread.
- **Is `1:3` reachable from a 2×-range stop?** The required move is `6 ×` the first-bar range.
  What fraction of sessions historically deliver that?
- **Is the 20-pip lock correctly scaled?** It is an absolute number on an instrument whose
  daily range varies by a factor of several; and it is compared against `S`, so the two regimes
  (`+20 pips` vs `breakeven`) switch on a threshold that has nothing to do with the trade's
  own R.
- **Is the one-bar fill delay material?** Entering 15 minutes after the session-open bar closes
  means the first (often largest) part of the session move has already happened, yet the stop
  is sized off that same bar.
- **Are unresolved pairs a hidden liability?** With no time exit, a quiet session's pair can
  sit for weeks accumulating swap and margin while its `3S` target drifts out of reach.
- **Is the whipsaw (`−2R` same-bar) frequency being underestimated** by closed-bar M15
  evaluation?
- **Does the paper mode measure anything about real execution?** It shares the backtest's fill
  model exactly, so it validates plumbing and state persistence, not fills.

---

## 15. Review agenda — questions this document is meant to get answered

Grouped so the review can be scoped. Priority order is roughly as listed.

**A. Does the edge exist?**
1. On XAUUSD M15 over several years, what is the empirical distribution of "post-session-open
   maximum favourable excursion measured in units of the first bar's range", per session? Does
   the `6 × range` move needed for a `1:3` target from a `2 × range` stop occur often enough
   for the pair to be profitable?
2. What is the realistic hit rate of each of the five pair outcomes in §4.6, per session and
   per volatility regime?
3. Does the strategy survive a full cost model — gold spread at each session open (including
   the wider Tokyo open), commission per leg, and two-sided overnight swap for multi-day
   holds?
4. Is the `bullish` label predictive of anything? Since the code takes both legs regardless, is
   there a version where the signal bar's direction earns a size or parameter tilt?

**B. Is the measurement trustworthy?**
5. How much do the Branch B same-bar assumptions (§4.8, §13.5) inflate results? Quantify by
   re-running against M1 data as an intrabar proxy.
6. What is the effect of the spurious first-bar signal (§13.6) on short-window backtests?
7. How should drawdown be measured for a strategy that holds unbounded numbers of overlapping
   pairs? Is closed-bar mark-to-market on `realized_pips + unrealized_pips` the right series,
   and what should the peak be initialised to?
8. Which metrics are missing for a decision — expectancy per pair, profit factor, MAE/MFE,
   time-in-trade distribution, per-session and per-weekday breakdown, R-multiple histogram?

**C. Risk and sizing**
9. Fixed `QTY` with range-proportional `S` means non-constant risk. What sizing rule fits —
   fixed-fractional on `S`, volatility-normalised, or a cap on `S` itself (`MAX_STOP_PIPS`)?
10. What exposure caps are needed — max concurrent pairs, max pairs per session, max total
    open risk?
11. Should there be a time-based exit (end of session, end of day, N bars, or an ATR-decay
    rule)? What does that do to expectancy?
12. Does the structure survive account realities — margin on two opposing legs, hedging vs
    netting accounts, FIFO jurisdictions?

**D. Parameter and regime work**
13. Sensitivity of `SL_MULT`, `RR`, `LOCK_PIPS`, `MIN_STOP_PIPS`, timeframe, and session
    windows, with an honest treatment of overfitting (walk-forward, out-of-sample split).
14. Should `LOCK_PIPS` become R-relative (e.g. lock at `0.25R`) rather than absolute?
15. Should the session windows be the *cash session* at all, or should the signal be anchored
    to a specific well-known open (e.g. the 08:00 London fix, the 09:30 NY equity open, the
    13:30 UTC US data window)?
16. Do the three sessions deserve independent parameter sets, and does the London/NY overlap
    need special handling?

**E. Engineering**
17. Fixes for §13.1–13.14, prioritised by whether they change results or only hygiene.
18. Should the engine expose a pluggable cost model and a pluggable fill model so that
    backtest optimism can be dialled explicitly?
19. What does this need before it could place real orders — order state machine, broker
    reconciliation, idempotency on restart, kill switch, and a live-vs-paper divergence
    monitor?
20. Data quality gates: gap detection, OHLC sanity, timeframe verification, and a policy for
    revised bars.

---

## 16. Quick source map for verification

| Concern | Location |
|---|---|
| Signal detection | `src/engine.py::_arm_signals` |
| Fill and pair construction | `src/engine.py::_fill_pending`, `_open_pair` |
| Exit decision tree | `src/engine.py::_manage_pairs` |
| Lock rule | `src/engine.py::_apply_lock` |
| Fill price model | `src/engine.py::_fill_stop`, `_fill_limit` |
| P&L, buckets, drawdown | `src/engine.py::_pnl*`, `_bucket`, `_record_equity` |
| Grouped pair reporting | `src/engine.py::_trade_pair_results`, `_pair_leg_result` |
| State persistence | `src/engine.py::snapshot`, `restore`; `src/paper.py::load`, `save` |
| Session windows | `src/sessions.py` |
| Candle sourcing / cache | `src/candles.py` |
| Routes, auth, overrides | `src/api.py` |
| Configuration | `src/config.py`, `.env.example` |
| Contracts | `src/models.py` |
