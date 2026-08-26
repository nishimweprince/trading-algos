# Backtesting service

Standalone FastAPI service for session-open entry structures. `ENTRY_MODE=hedge_pair` is the
incumbent: once per Tokyo / London / New York cash session, both a long and a short are simulated at
the configured entry time. Stop is `2 ×` the opening range by default; take-profit is 1:3. When one
side is stopped, the survivor moves to the configured absolute lock.

v1 is **backtest + paper**. It does not place orders — `submit_live_order()` in `src/mt5_live.py`
raises `LiveTradingDisabled` unconditionally, including when `LIVE_TRADING_AUTHORIZED` and
`TRADING_ENABLED` are both set. Clients talk only to this process; it pulls closed bars at the
configured `TIMEFRAME` from [execution-service](../execution-service/README.md). See
[Deployment](#deployment) for what running this on a server does and does not get you.

Paper and backtest share the same closed-bar engine. A paper fill is the next **closed** bar’s
open — the same as a backtest fill, which on H1 is up to an hour after a live open in wall-clock
time. This is not tick-level execution.

## Layout

`src/` is the package root (no nested package directory), matching `execution-service`. Default
HTTP port is **8012**. The backtest UI lives in `client/`.

## Setup

```bash
uv sync --all-packages
cd services/backtesting-service
cp .env.example .env
# set CTRADER_API_KEY to the running execution service's API_KEY on :8010
```

## Seed a local cache

Offline backtests need a handful of bars on disk. Fetch them from the gateway:

```bash
../../.venv/bin/backtesting-service --seed --symbol XAUUSD --timeframe M15 --count 2000
```

Writes `data/candles/XAUUSD/M15.jsonl` (gitignored). Seed the timeframe you intend to run — the
validated XAUUSD configuration is **H1**, not M15. Pytest uses the committed fixture under
`tests/fixtures/`.

## Run

```bash
../../.venv/bin/backtesting-service --validate-config
../../.venv/bin/backtesting-service --compare-entry-modes --symbol XAUUSD --timeframe H1
../../.venv/bin/backtesting-service --run-s8-scale-sweep --symbol XAUUSD
../../.venv/bin/backtesting-service --run-s1-target-hit
../../.venv/bin/backtesting-service --run-s2-break-frequency
../../.venv/bin/backtesting-service --run-s3-anchor-study
../../.venv/bin/backtesting-service --run-s4-cost-sensitivity
../../.venv/bin/backtesting-service --run-s9-regime-attribution
../../.venv/bin/backtesting-service --run-hedge-survivor-development --timeframe H1
../../.venv/bin/backtesting-service
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

`PAPER_ENABLED=true` (default) polls closed bars at the configured `TIMEFRAME` every
`POLL_INTERVAL_SECONDS`. On first start it **warms** to the latest bar and does not backfill
historical session entries. State is `logs/paper_state.json`. The paper loop takes its strategy
settings from `.env` only — see [the configuration trap](#the-configuration-trap).

## Deployment

### What a server deployment gives you

This service runs the strategy, and — when deliberately configured — places real orders on a
cTrader account through [execution-service](../execution-service/README.md).

`MARKET_EXECUTION_MODE` has three states and **defaults to `off`**:

| Mode | Behaviour |
|---|---|
| `off` | No bridge is constructed. The service is a simulation, exactly as before. |
| `shadow` | The exact order payload is built, recorded and shown on the live page, but nothing is sent. |
| `live` | Orders are submitted to `EXECUTION_CTRADER_ACCOUNT`. |

A service nobody configured cannot trade: `off` builds no client, and any other mode fails at
startup without an account alias — and `live` additionally requires `CTRADER_API_KEY`. Run
`shadow` for at least a full session cycle before `live`; it is the only way to see real payloads
without risking a fill.

### The configuration trap

**The UI sends strategy parameters with each backtest request. The paper/live loop reads only
`.env`.** A validated backtest proves nothing about what runs on the server unless the two agree.

The shipped `.env` now matches the validated XAUUSD H1 run (`ENTRY_MODE=oco_bracket`,
`TIMEFRAME=H1`, `OCO_BUFFER_VALUE=0.5`, `OCO_EXPIRY_BARS=1`, `SL_MULT=2`, `RR=3`, `LOCK_PIPS=20`,
`BE_TRIGGER_R=2`, `ENTRY_HOURS_UTC_EXCLUDE=13`, and the cost block). It previously resolved to
`hedge_pair` on `M15` — a different strategy on a different clock.

Verify before every deployment, and read the `resolved_configuration` line the service logs at
startup:

```bash
../../.venv/bin/backtesting-service --validate-config
```

Alternative configurations live in profiles rather than edits: `--profile NAME` reads `.env.NAME`.
`.env.shadow-demo` ships as the validated configuration with `MARKET_EXECUTION_MODE=shadow`. Real
environment variables outrank the file, so a one-off override needs no new file at all.

### Serving behind a proxy

The only additions a server needs beyond the shipped `.env`:

```bash
HOST=127.0.0.1
API_KEY=<generate one>          # required before /v1/execution is available
MARKET_EXECUTION_MODE=shadow    # then live, once you have watched a full session cycle
EXECUTION_CTRADER_ACCOUNT=forex_demo
```

If `API_KEY` is set, the client needs the same value as `VITE_API_KEY` at build time
(`client/.env.local`), and API callers send it as `X-API-Key`.

### Install on the server

```bash
git clone <repo> && cd trading-algos
uv sync --all-packages
cd services/backtesting-service
cp .env.example .env
# set CTRADER_API_KEY to the gateway's API_KEY, then apply the block above
```

Seed the local cache at the timeframe you will actually run, plus M1 so
`INTRABAR_MODE=m1_conservative` can resolve subpaths instead of silently falling back:

```bash
../../.venv/bin/backtesting-service --seed --symbol XAUUSD --timeframe H1 --count 10000
../../.venv/bin/backtesting-service --seed-m1 --symbol XAUUSD --count 20000
```

M1 coverage is reported on every run. Partial coverage is not an error — the resolver falls back
to `pessimistic_same_bar_no_subpath` and says so — but the fallback is what the current 2,000-bar
cache produces, and it is the conservative reading rather than the accurate one.

Build the UI into the API process if you want a single service to expose:

```bash
cd client && npm install && npm run build
```

### systemd unit

`execution-service` ships launchd plists for macOS (`infra/launchd/install.sh`); on a Linux server
use systemd. `/etc/systemd/system/backtesting-service.service`:

```ini
[Unit]
Description=backtesting-service backtest and paper engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trading
WorkingDirectory=/opt/trading-algos/services/backtesting-service
ExecStart=/opt/trading-algos/.venv/bin/backtesting-service
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now backtesting-service
journalctl -u backtesting-service -f
```

`logs/` and `data/` are written relative to `WorkingDirectory` and must already exist and be
writable by `User=` — systemd will not create them, and the process reads `.env` from that
directory too, so `WorkingDirectory` is not optional.

The service must start **after** `execution-service` is reachable, or `/health/ready` stays red until
the gateway answers. Paper state lives in `logs/paper_state.json` relative to `WorkingDirectory`;
back it up or accept that a restart warms to the latest bar without backfilling.

### Health and monitoring

| Check | Meaning |
|---|---|
| `GET /health/live` | Process is up |
| `GET /health/ready` | 200 only when execution-service `/health/ready` is 200 |
| `GET /v1/paper` | Open structures, last bar seen, recent events |

`/health/ready` is the one to alert on: it is the only signal that the upstream data feed is
alive. A paper engine with a dead feed reports no errors — it simply stops seeing bars.

On first start, paper **warms to the latest bar and does not backfill**. Expect no structures until
the next session anchor after start.

### Market execution

Orders go to [`execution-service`](../execution-service/README.md) over `POST /v1/orders`. It is
already this service's data feed, runs on the same host, and its operations are idempotent —
which matters because a restarted loop must not re-place a bracket it already placed.

The frozen pre-migration [`mt5-trader`](../../mt5-trader/README.md) service is not used here.
Neither is [`forex-execution`](../../forex-execution/README.md): its OANDA surface has no order
routes, only account and health routes.

```bash
MARKET_EXECUTION_MODE=shadow        # off | shadow | live
EXECUTION_CTRADER_ACCOUNT=forex_demo
EXECUTION_VOLUME_LOTS=0.01
EXECUTION_SOURCE=session_hedging
EXECUTION_TIMEOUT_SECONDS=10
EXECUTION_MAX_CONSECUTIVE_FAILURES=5
```

`EXECUTION_VOLUME_LOTS` is deliberately **not** derived from `QTY`. `QTY=1` means one standard
lot — about $10 per pip on gold — and is an accounting unit for the engine's P&L. What reaches the
broker is this setting alone, so a strategy change cannot alter position size by accident.

**How a structure becomes orders.** The engine stages an OCO bracket; the bridge places both sides
as pending stop-entry orders:

| Engine event | Broker action |
|---|---|
| `entry_order_staged` | Two `execution_type: stop` orders — buy at the upper trigger, sell at the lower |
| `entry_order_cancelled` (`oco_sibling`) | Cancel the losing side |
| `entry_order_cancelled` (`expired`) | Cancel both |
| `be_ratchet_armed` | `POST /v1/positions/protection` to move the stop |
| `prop_guard_breached` | Cancel every resting order and halt |

Four details are load-bearing:

- **Protection is sent as a distance, not a price.** The engine anchors an OCO stop and target to
  the *actual fill*, which only the broker knows. Absolute levels computed from a bar close would
  drift by the spread and by whatever price did between the decision and the trigger.
- **`occurred_at` is the moment of submission, not the bar timestamp.** The gateway rejects
  operations older than `SIGNAL_MAX_AGE_SECONDS` (60), and an H1 bar close is already an hour old.
- **`OCO_EXPIRY_BARS` is enforced twice.** The bridge cancels explicitly, and every order also
  carries a GTD `expires_at` one bar later — so if this process dies, the broker still cleans up.
- **A stale bar is refused outright.** If a bracket would already have expired before it could
  rest, its trigger levels are stale too, so the order is skipped rather than sent.

**Idempotency and restarts.** Each leg's `operation_id` is a UUIDv5 derived from
`symbol|pair_id|side`, so a restart mid-submit recomputes the same id and the gateway recognises
the retry instead of opening a second position. Broker order ids are persisted in
`logs/paper_state.json` (written atomically) because cancelling needs the integer `order_id`, not
the operation id. On startup the bridge resolves every pending operation with the gateway before
acting.

**Halting.** After `EXECUTION_MAX_CONSECUTIVE_FAILURES` unknown responses the bridge cancels its
resting orders and stops. A prop-guard breach only blocks *new* structures inside the engine —
orders already at the broker are untouched by it, which is why the bridge cancels them itself.

### Gateway prerequisites

`execution-service` ships fused off. Until you change these, every order returns 422 or 503:

```bash
# services/execution-service/.env.production
ALLOWED_ORDER_SOURCES=local,session_hedging   # else 422 source_not_allowed
TRADING_ENABLED=true                          # else 503 trading_disabled
# leave LIVE_TRADING_ENABLED=false and MAX_VOLUME_LOTS=0.01
```

The enabled demo alias with XAUUSD mapped is the intended target.
`forex_live` and `deriv_live` are `enabled = false` and invisible to the API. Execution routes only
exist when `ACCOUNTS_CONFIG_PATH` is set. Confirm `GET /health/trading-ready` returns 200 before
switching to `live` — the live page surfaces this as the gateway badge.

Run in `shadow` for a full DST cycle before going live. The validated configuration excludes 13:00
UTC, which removes New York for the EDT half of the year, so a summer-only observation window will
not show you the winter behaviour.

### Live performance page

The client has two views, switched in the sidebar: **Backtest** and **Live**. The live view polls
`GET /v1/paper` and `GET /v1/execution` every 15 seconds and shows realised P&L, the equity curve,
session and day breakdowns, the closed-structure blotter, and an engine-versus-broker divergence
panel with per-fill slippage.

`GET /v1/execution` is guarded by a stricter dependency than the rest of the API: where the normal
`authenticate` fails *open* when no `API_KEY` is configured — acceptable for a read-only backtest
API — the execution route refuses with 503 instead. An unconfigured key is not a waiver for a route
that exposes broker state.

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
| GET | `/health/ready` | 200 when execution-service `/health/ready` is 200 |
| GET | `/v1/config` | Form defaults (symbol, sessions, risk). No secrets |
| GET | `/v1/candles` | Local file or gateway proxy (`source=local\|ctrader`) |
| POST | `/v1/backtests` | Run the engine; `source` defaults to local if the cache exists |
| POST | `/v1/backtests/compare` | Run the same candle fingerprint and shared parameters through all four entry modes |
| GET | `/v1/paper` | Open pairs, closed structures, equity curve, last bar, recent events |
| GET | `/v1/execution` | Execution mode, gateway readiness, tracked orders, broker positions, divergence. Requires a configured `API_KEY` |

`POST /v1/backtests` accepts the strategy fields above plus Phase 1 cost overrides. Every override
is rebuilt and revalidated as a complete engine configuration before the run starts.
Successful backtests expose the resolved survivor/path settings and candle fingerprint. The UI's
settings download is schema version 2 and preserves both the immutable submitted request and the
fully resolved engine configuration.

## Entry modes

`ENTRY_MODE=hedge_pair` names the Phase 1 incumbent explicitly. Its construction passes through
`src/entry/hedge_pair.py`; the golden parity test binds its complete ordered trades/events and
statistics to the committed pre-refactor fixture from `59eaf05`.

Hedge-pair survivor management is additive and defaults to historical parity:
`SURVIVOR_EXIT_MODE=legacy_lock` with `HEDGE_PATH_MODE=legacy_parent_bar`. Opt-in research runs can
use `unlocked` or `mfe_trail` with `chronological_v2`. The chronological resolver begins survivor
decisions only after the first stopped leg, arms a new MFE stop for the next child/parent bar, and
records every incomplete-M1 fallback. MFE activation and gap are independently configured in R;
they do not reuse or alter OCO's `BE_TRIGGER_R`.

`--run-hedge-survivor-development` runs the frozen ten-candidate H1 family through both normal
portfolio and common-signal matched-opportunity replays under base and doubled spread/slippage.
Its JSON artifact includes the candidate hash, overlap attribution, selection result, and a locked
external-holdout status. It never promotes a candidate or opens the required three-year holdout.

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

**Pips versus dollars is chosen in the UI, per run, not in `.env`.** The engine computes in pips
because pips are what the price data supports; the unit you pick decides how results are reported.
Selecting **Dollars** reveals a dollar-per-pip rate at `QTY=1`, defaulting to **10**:

```text
dollars = pips × rate × QTY
```

Every additive metric of a run is returned once, already in the selected unit, in the report's
`performance` block: realized, unrealized and equity (gross, cost and net), execution and financing
cost, maximum drawdown, break-even cost per completed side, and the configured per-side costs. The
block also states `unit`, `dollars_per_pip_per_qty` and the single `conversion_factor` applied, so
any number in it can be checked by hand. **R multiples are never converted** — R is a ratio, and it
is reported alongside the unit amount everywhere.

The pip-denominated fields (`gross_equity_pips`, `net_realized_pips`, …) remain on the report
unchanged whatever unit is selected, so the raw series and the CSV export stay comparable across
runs. Requests carry `performance_unit` and an optional `dollars_per_pip_per_qty`; `/v1/config`
publishes `default_dollars_per_pip_per_qty` so the UI knows the default without hard-coding it.

The rate is also the cash conversion the engine itself uses. `RISK_MODE=fixed_fractional` and
`FIRM_PROFILE=custom` size in account currency regardless of how results are displayed, so they
receive the same rate — the UI's when a request supplies one, otherwise the default 10.

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

`RISK_MODE=fixed_qty` preserves `QTY`. `fixed_fractional` sizes one R to `RISK_PCT_PER_R` percent
of current marked equity, converting at the dollar-per-pip rate described above. Its
denominator is `S + 2 × SLIPPAGE_PIPS_PER_SIDE`, so entry and stop-exit slippage cannot understate
risk. `MAX_PAIR_RISK_PCT` caps the new pair. `MAX_OPEN_RISK_PCT` rejects a new pair rather than
resizing any open pair. `ONE_OPEN_PER_SESSION` and `MAX_CONCURRENT_STRUCTURES` reject excess
structures; the report exposes the total and reason counts for suppressed signals.

`FIRM_PROFILE=none` keeps the parity path. `custom` enables PropGuard and converts at the same
dollar-per-pip rate. The guard evaluates marked equity—including floating P&L—against the
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

Gateway candles are stamped at the **end** of the UTC interval. Session membership uses **bar open**
(`ts − TIMEFRAME`) so the bar ending at the anchor is not treated as the open — on M15 the New York
07:45–08:00 bar, on H1 the 07:00–08:00 bar.

Default gold pip size is `0.1`.

## Resolver calibration

`../../.venv/bin/backtesting-service --run-s5-resolver-bias` runs one immutable configuration through tiers 0–3 and
reports tier 4 as the tick-source interface. The current descriptive M15 calibration, on the same
2,000-bar fingerprint used by S1–S4/S8/S9, is:

| Tier | Resolver | Gross / net pips | Gross / net R | Delta vs tier 0 | Changed structures |
|---:|---|---:|---:|---:|---:|
| 0 | optimistic | 657.90 / 657.90 | 0.8680 / 0.8680 | 0.00 pips / 0.0000R | 0 |
| 1 | pessimistic | 727.70 / 727.70 | 1.2911 / 1.2911 | +69.80 pips / +0.4230R | 1 |
| 2 | M1 | 727.70 / 727.70 | 1.2911 / 1.2911 | +69.80 pips / +0.4230R | 1 |
| 3 | M1 conservative | 727.70 / 727.70 | 1.2911 / 1.2911 | +69.80 pips / +0.4230R | 1 |
| 4 | tick | unavailable | unavailable | unavailable | unavailable |

M1 coverage is partial (93/2,000 parent bars), so tiers 2 and 3 uniformly fall back to
`pessimistic_same_bar_no_subpath`; no partial chronology is mixed. This is a harness calibration,
not a universal strategy constant. The one changed London structure moves from a −1.3018R whipsaw
under tier 0 to a −0.8788R lock under tiers 1–3, explaining the locally inverted ladder shape.
The §0 export rates (M15 10.6%, H1 11.2%, H4 5.1%) remain unverified until the named export CSVs
are supplied.

## Tests

```bash
.venv/bin/pytest
cd client && npm test
```

The non-fixture Phase 5 verification adds an auditable report header, paired structure-level
gross/net R, R/holding/excursion/concurrency diagnostics, session and weekday tables, deterministic
fill properties, and a 32-cell executable configuration matrix. See
`reports/research/phase5-non-fixture-verification.md`. Phase 5 remains incomplete: five historical
export checks are explicit skips until the named M15/H1/H4 CSVs are supplied.
