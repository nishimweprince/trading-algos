# Session-hedging

Standalone FastAPI service for session-open entry structures. `ENTRY_MODE=hedge_pair` is the
incumbent: once per Tokyo / London / New York cash session, both a long and a short are simulated at
the configured entry time. Stop is `2 ×` the opening range by default; take-profit is 1:3. When one
side is stopped, the survivor moves to the configured absolute lock.

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
session-hedging --compare-entry-modes --symbol XAUUSD --timeframe H1
session-hedging --run-s8-scale-sweep --symbol XAUUSD
session-hedging --run-s1-target-hit
session-hedging --run-s2-break-frequency
session-hedging --run-s3-anchor-study
session-hedging --run-s4-cost-sensitivity
session-hedging --run-s9-regime-attribution
session-hedging
```

The comparison command reads one local candle range and runs its fingerprinted, immutable input
through `hedge_pair`, `synthetic_breakout`, `contingent_hedge`, and `oco_bracket`. Optional
`--date-from` / `--date-to` values are inclusive ISO-8601 timestamps with timezone offsets. It
prints one JSON report; no mode can silently use a different date range, cost model, sizing rule,
resolver, stop rule, or risk configuration.

`--run-s8-scale-sweep` is the offline S8 research harness (§10 of the specification). It reads one
immutable local **M15** candle set and runs the complete 256-cell scale grid — four entry modes x
`ORB_MINUTES` {15, 30, 60, 120} x `ENTRY_DELAY_MINUTES` {0, 15, 30, 60} x `MAX_AGE_HOURS`
{8, 12, 24, 48}, all with `TIME_EXIT_MODE=max_age` — writing
`reports/research/s8-scale-decomposition.json` and its rendered `.md`. Every cell shares one candle
fingerprint and one configuration; only those four fields vary, and each cell is validated rather
than copied unchecked. The output states whether covering M1 data existed and, when it did not,
names the conservative no-subpath fallback the resolver used instead. It is descriptive
measurement: it reports the whole surface, losing cells included, and selects nothing.

The remaining research commands are the §10 studies. Each reads the same immutable local M15
candle set, embeds the candle fingerprint and the M1-coverage state, and writes
`reports/research/<study>.json` plus a rendered `.md`:

| Command | Study | Question |
|---|---|---|
| `--run-s1-target-hit` | S1 | The probability that the survivor reaches `kR` within a horizon, given the first stop occurred, by session, horizon and ATR tercile, with MFE/MAE distributions. Reach is measured beyond the configured `RR`, because a run censored at `3R` cannot justify `4R` |
| `--run-s2-break-frequency` | S2 | How often one side of the opening range breaks and the other is never tested, by session, weekday and contraction tercile, priced against all four entry modes |
| `--run-s3-anchor-study` | S3 | The §4.1 anchor grid, one anchor at a time, with range and tick-volume expansion against the window before the anchor. Is New York's negative result an anchor problem? |
| `--run-s4-cost-sensitivity` | S4 | Spread, slippage and commission per side, per mode, against the §9 requirement of 2x break-even headroom |
| `--run-s9-regime-attribution` | S9 | Calendar half, trend regime and session splits, plus the long-versus-short split of surviving winners, flagging any edge that leans on one direction |

All of them are descriptive. They report full surfaces including losing cells, select no
parameters, and are gated by §9 before anything they measure can drive a change.

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
| POST | `/v1/backtests/compare` | Run the same candle fingerprint and shared parameters through all four entry modes |
| GET | `/v1/paper` | Open pairs, last bar, recent events |

`POST /v1/backtests` accepts the strategy fields above plus Phase 1 cost overrides. Every override
is rebuilt and revalidated as a complete engine configuration before the run starts.

## Entry modes

`ENTRY_MODE=hedge_pair` names the Phase 1 incumbent explicitly. Its construction passes through
`src/entry/hedge_pair.py`; the golden parity test binds its complete ordered trades/events and
statistics to the committed pre-refactor fixture from `59eaf05`.

`ENTRY_MODE=synthetic_breakout` is its payoff-matched control. At reference entry `E`, it stages
OCO stop entries at `E ± S`; only the triggered side fills. Its stop and target remain at the
absolute levels the hedge survivor would have after the first stop, so a no-gap path has identical
gross payoff while normally using two transaction sides rather than four. A trigger gap fills at
the bar open. Pending OCO orders persist in paper state, reserve risk/concurrency, and have no cost
until filled. `TP_MODE=fixed_r` and `LOCK_MODE=absolute` name the shared target/lock semantics.

`ENTRY_MODE=contingent_hedge` uses the same primary OCO. `HEDGE_RATIO_INITIAL=0` delegates to the
synthetic path; `1` delegates to hedge-pair at `E`. An intermediate ratio opens that fraction of
both legs at `E`, stops the opposite tranche at the breakout, and scales the survivor to full
quantity at the actual trigger fill. With a directional primary, `failure_zone` stages the
opposite hedge at `E + S - HEDGE_FAILURE_K × S` for a long (mirrored for a short), up to
`HEDGE_RATIO_STAGED`. Ratios are from zero to one and staged ratio cannot be below initial ratio.
Actual fill counts and quantity-weighted side equivalents are reported separately.

`ENTRY_MODE=oco_bracket` stages stop entries at opening-range high plus a buffer and opening-range
low minus a buffer. `OCO_BUFFER_MODE=orb_frac` interprets `OCO_BUFFER_VALUE` as a fraction of the
measured range; `fixed_pips` multiplies it by `PIP_SIZE`. A trigger gap fills at the bar open, and
the stop and `RR` target are then measured from that actual fill. An unfilled bracket expires after
exactly `OCO_EXPIRY_BARS` eligible parent bars. `ALLOW_REENTRY=true` permits one fresh, tagged
bracket after a filled structure closes; a re-entry can never stage another re-entry.

The four-mode comparison reports paired gross/net pips and R, paired expectancy/profit-factor/win
statistics, costs, drawdowns, survivor break-even metrics, hold-time percentiles, actual and
quantity-weighted sides, risk suppressions, unresolved structures, and PropGuard state. Headline
gross/net values are final marked equity; expectancy and profit factor use completed structures.
Its hedge-minus-synthetic attribution partitions gross difference into explicitly tagged gap,
same-bar, and residual payoff buckets, then subtracts cost difference to reconcile to net.

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

Costs are configured in pips per transaction side. `SPREAD_PIPS_PER_SIDE`,
`SLIPPAGE_PIPS_PER_SIDE`, and `COMMISSION_PIPS_PER_SIDE` apply to entries and exits. Long/short swap
rates accrue at `SWAP_ROLLOVER_TIME` in `SWAP_TIMEZONE`; Wednesday is triple by default and weekend
days are not charged again. `SESSION_COST_OVERRIDES` is a JSON mapping of session names to partial
numeric schedules. `COST_MODEL=none` is the explicit parity control.

Reports preserve the former `realized_pips`, `realized_r`, and drawdown fields as gross aliases and
also return paired `gross_*`, `cost_*`, and `net_*` pip/R totals. The break-even panel reports gross
expectancy per transacted side and its ratio to configured spread; the Phase 1 decision gate
requires at least 2× headroom.

`RISK_MODE=fixed_qty` preserves `QTY`. `fixed_fractional` requires
`DOLLARS_PER_PIP_PER_QTY` and sizes one R to `RISK_PCT_PER_R` percent of current marked equity. Its
denominator is `S + 2 × SLIPPAGE_PIPS_PER_SIDE`, so entry and stop-exit slippage cannot understate
risk. `MAX_PAIR_RISK_PCT` caps the new pair. `MAX_OPEN_RISK_PCT` rejects a new pair rather than
resizing any open pair. `ONE_OPEN_PER_SESSION` and `MAX_CONCURRENT_STRUCTURES` reject excess
structures; the report exposes the total and reason counts for suppressed signals.

`FIRM_PROFILE=none` keeps the parity path. `custom` enables PropGuard and requires the explicit
dollar-per-pip conversion. The guard evaluates marked equity—including floating P&L—against the
daily reset reference and initial-balance total-loss floor. A breach is sticky, persists in the
paper snapshot, and blocks new structures; it never force-closes positions or rewrites history.

`TIME_EXIT_MODE=max_age` closes any surviving leg at the first completed bar close strictly after
`entry_ts + MAX_AGE_HOURS` (24 hours by default). If that bar also touches a stop or target, the
configured intrabar resolver chooses the level fill before the close-time exit is considered.
Time exits have a dedicated outcome-mix bucket.

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
