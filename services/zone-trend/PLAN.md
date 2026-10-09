# Zone-Trend implementation plan (spec v1.1)

## Context
Spec: `~/Documents/Markets/Docs/Zone Trend/Zone-Trend Strategy Spec v1.1.md`. The goal is a new autonomous Binance USDⓈ-M perps swing bot:
- 1D/4H zones and trend, with 1H break-and-retest entries.
- Sizing by % price movement.
- Its own Binance Vision backtester.
- Shadow, then staged live.

Deploy is gated: M5/M6 (live) are built only if the M4 walk-forward passes the acceptance criteria and beats the EMA 20/100 benchmark.

Decisions taken with the user:
- **Location:** `services/zone-trend/`, package `zone_trend`. It joins the uv workspace via the `services/*` glob and follows the ofi-scalper-service conventions.
- **Branch:** `feat/zone-trend`, cut from `main`.
- **Exec path:** extend `plugins/binance-futures` with algo orders, account setup and leverage brackets. zone-trend obtains the plugin through `ta_plugin_api.load_providers` and runs the spec's own OMS, reconciler and SQLite. execution-service is not changed.
- **Rejection candle:** the zone-side wick is at least 40% of the range and the close is in the outer third of the bar, outside the zone. Both thresholds are configurable.
- **TP2:** the nearer of the next opposing 1D/4H zone and 3R. The trail runs alongside it.
- **Golden set:** the user labels it later. We ship a `zt episodes` CLI that renders candidates for labelling.
- **Host:** a new small Tokyo/SG VPS (deploy kit at M6).
- **Account:** sub-account or main is undecided. Equity and drawdown are computed from zone-trend's own positions so either works.

## Spec review: issues to resolve in code (no spec change needed unless noted)
1. **BTCUSDT minNotional is about $100** (ETH about $20, most alts $5; verify from exchangeInfo).
   - At $500 equity, the 40% TP1 partial on BTC is almost always below 1.1 × minNotional, so the spec's single-TP fallback triggers.
   - BTC trades with stops ≥ ~2.4% fall under minNotional and are skipped.
   - The backtest must report these skips explicitly by reason.
2. **Historical exchangeInfo and leverage brackets aren't on Binance Vision.**
   - Use the current snapshot for listed symbols and conservative defaults for delisted ones.
   - Record the approximation in every run's metadata.
3. **Warm-up eats history.** It is 365d for 1D zones plus a 1-year volatility percentile. With data from 2020-01, the first tradable bar is around 2021-01, and the first walk-forward in-sample window shifts accordingly.
   - Sync from the earliest listing, which is 2019-09 for BTC.
4. **The k_sym displacement stats must come only from data before each walk-forward in-sample window ends,** or the backtest leaks future information. Before 30 trades, k_sym = 1.0.
5. **Retracement at retest time.** The spec says retr uses impulse A→B, but B (the 1H swing high after the break) is often not confirmed yet at the retest.
   - Interpretation: A = the last confirmed 1H opposite swing before the breakout; B = the running extreme since the breakout. Both are known at bar close, so there's no look-ahead.
   - Documented in `fib.py`.
6. **Funding blackout.** Entries fill at 1H opens and settlements fall on the hour, so "15 min before settlement" never binds on market entries.
   - It is kept for limit-entry validity windows.
   - Funding is normalised to its 8h equivalent for 4h-interval symbols.
7. **Same-minute stop/TP conflicts.** v1 assumes the stop is hit first and counts conflicts per run. An aggTrades replay is added only if conflicts exceed 1% of exits, which saves pulling tick data up front.
8. **Zone strength components** (touch scaling, departure size, recency decay) are not fully defined.
   - Each one gets an explicit formula in `zones.py` with a docstring and unit tests, exposed in config.
   - Strength stays a ranking signal. Only the ≥70 Range rule depends on its absolute value.
9. **Algo Order API** is confirmed in current docs as `POST /fapi/v1/algoOrder` with `algoType=CONDITIONAL`, `STOP_MARKET`/`TAKE_PROFIT_MARKET`, `workingType`, `priceProtect`, `closePosition` and `clientAlgoId` (≤36 chars).
   - The spec's `zt-{hash12}-{leg}` id fits.
   - Re-check the user-stream event type for algo orders at M5.
10. **The roadmap diagram is missing from the spec text.** The milestones below are inferred from it.

## Package layout (`services/zone-trend/`)
The spec's layout is kept, with these adaptations:
- `pyproject.toml` uses hatchling with `packages = ["src/zone_trend"]` and ruff `extend = "../../pyproject.toml"`.
- Workspace deps: `ta-core`, `ta-notify`, `ta-plugin-api`, `ta-plugin-binance-futures`.
- Other deps: numpy, pandas, pyarrow, pydantic v2, pydantic-settings, SQLAlchemy 2, httpx, structlog (or ta-core logging), hypothesis (dev).
- `config/default.yaml` holds the spec's config verbatim, loaded by `config.py` (pydantic, range-checked).
- Secrets and notification settings come from env via `ta_core.BaseServiceSettings + ta_notify.NotificationSettings + BinanceFuturesSettingsMixin`. This is the same mixin set as `ofi_scalper_service/config.py`.
- Package directories follow the spec: `src/zone_trend/{types,config}.py`, `data/`, `core/`, `risk/`, `backtest/`, `live/`, `persistence/`, `cli.py`. The CLI is `zt`, a `[project.scripts]` entry.
- `tests/{unit,property,lookahead,golden,integration}`, with a pytest config mirroring ofi: `asyncio_mode=auto`, `filterwarnings=error`, and an `integration` marker.
- No GitHub Actions CI (main removed every workflow on 2026-10-09; GitGuardian is the only PR check). Before pushing: `ruff check`, `ruff format --check`, `pytest -m "not integration"`, and `python3 infra/check_plugin_boundary.py`.

## Reuse (port or import, don't rewrite)
| Need | Source |
|---|---|
| Vision listing, download, SHA-256 checksum | `services/ofi-scalper-service/research/collect/vision.py` (copy into `zone_trend/data/vision.py`; add `klines`, `markPriceKlines` and `indexPriceKlines` with the interval path segment, plus unzip → Parquet) |
| Volume profile HVN | `vrvp-strategy/indicators/volume_profile.py::_calculate_profile` (port vectorised into `core/indicators.py`, past window only, HVN as clustered peaks) |
| PF, percentiles, Wilson | `services/backtesting-service/src/backtesting_service/harness/metrics.py`, `cell_stats.py` |
| Data fingerprint | `.../harness/fingerprint.py` pattern for `snapshot_id` |
| Block bootstrap MC, DSR/PBO | `.../research/s7_prop_monte_carlo.py`, `research/s6_walk_forward.py` (generic parts) |
| Report tables | `.../research/markdown.py` |
| Signed REST, weight limiter, filters, user stream, cancel-all/flatten | `plugins/binance-futures`: `FapiRest` (`rest.py`), `OrderFilters`/`load_filters` (`instruments.py`), `UserStream`, `AccountReader`, `OrderRateGovernor`, `FuturesStreams` (markPrice@1s) |
| Alerts | `ta_notify.Notifier.send` (never raises) |
| Test fakes | `ta_plugin_binance_futures.testing` (`FakeBinanceFutures`, `FakeFuturesStream`), ofi `conftest.py` patterns |

## Milestones (each ends with tests green and a commit)

**M1: Data pipeline**
- `data/vision.py`: monthly archives for closed months, daily for the current one, with checksum verification. Header detection by whether the first field is numeric; int64 ms timestamps.
- `data/validate.py`: the six checks plus a quarantine list.
- `data/store.py`: zstd Parquet, a manifest, `snapshot_id`.
- `data/universe.py`: monthly top 10, survivorship-safe (the symbol list comes from the S3 listing, so delisted symbols are included).
- CLI: `zt data sync|validate|universe`.
- Datasets: klines (1m/1h/4h/1d) and markPriceKlines (1m/1h) for the top ~20 candidates; fundingRate; exchangeInfo and leverageBracket snapshots via the public/plugin REST.

**M2: Pure core**
- `core/indicators.py`: Wilder ATR, EMA, volume median, correlation, HVN. Incremental functions must equal the vectorised reference within 1e-9.
- `swings.py`: L/R confirmation, k×ATR leg, alternation collapse, `t_conf`.
- `structure.py`: trend state, BOS, CHoCH, normalised slopes, momentum decay.
- `zones.py`: build, merge, score, break/flip, delete, keep 6 per side.
- `fib.py`, `retest_fsm.py` (states IDLE→BROKEN→RETESTING→ARMED→ENTERED/FAILED), `signals.py` (content-hash ids), `exits.py`.
- `on_close(state, new_bars, cfg)` follows the spec's 7-step order.
- Tests:
  - Unit tests per rule.
  - Hypothesis property tests (truncating the future never changes the past).
  - **Look-ahead harness:** run cut at T vs the full run, and HTF shift tests.

**M3: Risk and backtester**
- `risk/sizing.py`: both modes; the worked-example table as a test.
- `risk/liquidation.py`: bracket MMR, and the stop must be ≤ 0.5 × liquidation distance.
- `risk/portfolio.py`: limits, correlation caps, daily/weekly/DD/consecutive-loss breakers.
- `risk/displacement.py`: k_sym.
- `backtest/broker_sim.py`:
  - next-open fills ± slippage;
  - limit through-by-one-tick;
  - mark-1m stops at 2× slippage;
  - last-1m TPs;
  - maker/taker fees;
  - funding at actual settlement timestamps;
  - step/tick/minNotional;
  - liquidation raises.
- `backtest/engine.py` follows the spec loop.
- Outputs go to `runs/{id}/` as config, snapshot, and trades/equity/signals Parquet (rejected signals with reasons), plus `report.html`.
- An EMA 20/100 benchmark through the same engine.

**M4: Validation (GATE)**
- `walkforward.py`: 18m in-sample / 6m out-of-sample / 6m step, a ±20/40% grid, an objective of median-of-3-folds PF·√n with a DD penalty, stitched out-of-sample results.
- Holdout: the last 9 months, run once.
- `montecarlo.py`: 10k block bootstrap and a one-bar delay variant.
- `zt ablate` covering the volume filter, fib band (0.382–0.786 vs 0.3–0.8), slope, funding and HTF zones.
- 1×/2×/3× slippage runs.
- `zt episodes` renders candidates for the user's golden labels.
- **Stop here and report.** If the criteria fail, rework the rules or retire the strategy; M5/M6 are not built.

**M5: Live adapters and shadow** (only after M4 passes)
- `plugins/binance-futures` additions, with fakes extended in `testing.py` and plugin tests:
  - `algo.py`: place/cancel/list `algoOrder`;
  - `account_setup.py`: `positionSide/dual` check, marginType ISOLATED, leverage, `leverageBracket`;
  - kline 1h/4h/1d streams (or a REST poll 5s after close, confirming the kline is final);
  - algo events on `UserStream`.
- Boundary rule: zone-trend gets these through the factory, never by direct construction (`infra/check_plugin_boundary.py`).
- `live/oms.py`: intent persisted before the call, deterministic client ids, query-before-resend on ambiguous responses, place-then-cancel stop moves, and close-if-stop-fails-twice.
- Also `reconciler.py`, `runner.py` (startup sequence, hourly scheduler) and `notify.py`.
- `persistence/`: the spec's SQLite WAL schema.
- `zt shadow`, `zt live [--resume]`, `zt kill`.
- Integration tests with mocked REST/WS: ambiguous send, -2021, partial fills, reconnect, reconciler repairs.
- Parity: replaying a shadow week through the backtester must give ≥95% identical signals.

**M6: Ops and rollout**
- Prometheus `/metrics`, `/health` for an external uptime monitor, heartbeat, daily summary, weekly live-vs-backtest report.
- systemd units following `infra/systemd/ofi-*` (OnFailure alert unit).
- Deploy notes for the new Tokyo/SG VPS (IP-whitelisted futures-only key, off-site DB backup, lock file).
- Stage-gate automation for Shadow → Micro → Half → Full.

## Immediate first step (on approval)
1. `git checkout -b feat/zone-trend` from `main`.
2. Scaffold `services/zone-trend/`:
   - `pyproject.toml`, `README.md`;
   - `config/default.yaml` from the spec;
   - `config.py` with a validation test;
   - `types.py` and an empty module tree.
3. Copy this plan into `services/zone-trend/PLAN.md`, with the spec-review issues as tracked items.
4. Run `uv sync --package zone-trend --group dev`, `ruff`, `pytest`. Commit.
5. Begin M1.

## Verification
- Every milestone: run `uv run --package zone-trend pytest -m "not integration"` and `ruff check` + `ruff format --check` (same as CI).
- M1: `zt data sync --symbols BTCUSDT,ETHUSDT --from 2020-01 ...`, then `zt data validate` gives a clean manifest and quarantine report.
- M2: the look-ahead suite passes (cut-at-T equality on BTC/ETH); a visual spot check via `zt episodes`.
- M3: the sizing table test passes; a short `zt backtest` run produces `runs/<id>/report.html` with costs, funding and skip reasons.
- M4: `zt wf`, `zt mc`, `zt ablate` produce an acceptance-criteria table (pass/fail per row) and the benchmark comparison. **Go/no-go is decided with the user.**
- M5: plugin and zone-trend integration tests against the fakes; a 2-week shadow run with the parity replay at ≥95%.
- Pre-flight before any live stage: Futures API is enabled on the Rwanda account; the key is futures-only with withdrawals disabled and IP-whitelisted.

## Progress log

### M1 data pipeline (2026-10-09)
Built: `data/vision.py` (S3 listing, month planner, checksum-verified atomic downloads), `parse.py`, `store.py`, `sync.py`, `validate.py`, `exchange.py`, `universe.py`, and the `zt data symbols|sync|exchange-info|validate|universe` commands.

Decisions and findings from real data (BTC/ETH, 2019-09 to 2026-10):
- **Storage.** Zips are kept under `data/raw` (pass `--drop-raw` to delete them). Parquet month files use zstd level 9 with delta-encoded integer columns, and datetimes are derived on load. That makes them 393 MB against 474 MB of zips; the default encoding was 875 MB.
- **Month planner.** Monthly archives are used where they exist and daily archives otherwise. A month built from dailies is rebuilt once its monthly archive is published, and never touched after that.
- **Last-price klines are complete:** no gaps and no OHLC errors. The only defects are 1m volume losses around exchange incidents (2021-05-15, 2023-11-10, 2025-01-29, ...), which leave 10 quarantined days per symbol.
- **Mark and index klines on Vision miss whole days** (BTC 2019-12-25..30, 2021-07-24..27, 2022-07-31, ...). These are **flagged, not quarantined**:
  - nothing in the strategy reads index prices;
  - the M3 broker must fall back to last-price 1m for stop triggers where mark data is missing, because positions stay open through no-trade days anyway.
  - `zt data validate --strict` gives the spec's literal reading.
- **Mark/last divergence > 2%** is flagged, not quarantined (only 2020-03-13 on BTC), so crash days are not dropped from the backtest.
- **Funding.** `calc_time` carries 1–2 ms of jitter; `settle_time` rounds it to the second.
- **Universe.**
  - The candidates are every USDT perpetual on Vision (delisted ones included), minus stablecoin bases and dated contracts.
  - Pool = the top 3 × size by 30-day median quote volume. The range filter is applied against that pool's median.
  - tickSize comes from the current exchangeInfo snapshot, so a delisted symbol skips the tick check and is listed in `no_tick`.
- **Leverage brackets** need a signed request, so they are deferred to M5. The backtester uses conservative defaults.

### M2 pure core (2026-10-09)
Built: `core/indicators.py`, `series.py`, `swings.py`, `structure.py`, `zones.py`, `fib.py`, `retest_fsm.py`, `signals.py`, `exits.py`, `symbol.py` (`SymbolCore.on_close` in the spec's 7-step order) and `replay.py`.

Tests:
- Unit tests for each rule.
- The incremental ATR and EMA match the vectorised reference within 1e-9.
- Look-ahead (`tests/lookahead`):
  - truncation invariance under hypothesis;
  - future 4H/1D bars perturbed with no effect on the past;
  - real BTC truncated at 2023-01-01 (marker `data`, skipped without data).
  - The harness was checked against an injected leak, feeding 4H bars 3h early; the leak was detected.

Interpretations, beyond those decided with the user:
- **Swing leg:** measured from the last kept swing's pivot (or the start of the L window) to the pivot. A swing replaced by a more extreme one takes the later `t_conf`.
- **CHoCH:** it forces Range until a newer swing is confirmed, and only blocks the side it broke. BOS is recorded as an event only.
- **Slopes:** they use 4H legs. Momentum decay compares the latest impulse with the median of the three impulses *before* it.
- **Zone strength scales** are documented in `zones.py`. Zone state (role, flips, breaks, deep retests, later touches) carries across rebuilds via the ancestor that shares the most seed swings. Deleted zones' seeds never reseed.
- **New zone role** comes from its position against the close.
- **Breakouts** must cross the zone edge (the previous close was inside or below it). Longs break resistance and shorts break support.
- **Stop anchor:** `exits.stop_anchor` (`retest` by default, or `zone` / `further`).
- **TP2 equal to or below TP1** is dropped, and the runner is left to the trail.
- **Volatility filter** needs at least 180 daily values (the spec says a 1-year window).
- **Entry types A and C** are refused at config load; only B is built.
- Funding, correlation and the BTC gate are portfolio-level checks and move to M3 (risk).

BTCUSDT smoke run over the full history (59k 1H bars, 12 s):
- 4H trend: Range 63% of hours, Up 21%, Down 16%.
- 29 signals (19 long, 10 short) and 67 rejections. By reason: slope 23, no_room 17, vol_filter 11, vol_history 8, stop_too_wide 5, momentum_decay 2, trend 1.
- About 4 signals a year on BTC is thin against the M4 requirement of ≥15 out-of-sample trades per symbol. Watch trade counts once the full universe is in.

### M3 risk and backtester (2026-10-09)
Built:
- `risk/sizing.py`, `liquidation.py`, `displacement.py`, `portfolio.py`.
- `backtest/tape.py`: the per-symbol core replay, parallel and cached under `data/cache/tapes`. Also the EMA 20/100 benchmark tape.
- `backtest/market.py`: 1m last and mark minutes, loaded a month at a time and only for symbols with orders. Also funding.
- `backtest/broker_sim.py`, `engine.py`, `metrics.py`, `report.py`.
- The `zt backtest` command (`--benchmark`, `--slippage-mult`, `--symbols` or the universe history).
- Outputs in `runs/{id}/`: config.yaml, snapshot_id, meta.json with a summary, trades/fills/equity/signals Parquet (every rejection with its stage and reason), and report.html.

Tests:
- The spec's sizing worked-examples table.
- Liquidation distance at 5x.
- Breakers and each entry check.
- Broker scenarios: fills, slippage, a gap through the stop, TP1 → breakeven, TP2, single TP, same-minute conflict, mark-triggered stop, funding sign and timing, liquidation.
- An end-to-end engine run on synthetic data through the real store. With everything flat at the end, final cash = start + Σ net, and cached reruns are identical.

Interpretations:
- `k_sym` scales the risk budget *inside* the margin cap (the spec's formula multiplies after the `min`, which could exceed the cap).
- A "single take-profit" closes the whole position at TP1.
- The breakeven stop moves at the TP1 fill, mirroring the OMS.
- Same-minute stop/TP resolves as the stop, and conflicts are counted.
- The funding filter uses the last settled rate, 8h-normalised.
- The weekly breaker anchors at Monday 00:00 UTC.
- Rejected signals on quarantined days get reason `quarantine`; days flagged for mark/last divergence get `data_sanity`.

Smoke run, BTC+ETH 2021-01..2026-09, $500:
- 43 trades, PF 1.23, +0.03R per trade, max DD 1.5%, Sharpe 0.26. At 2x slippage, PF is 1.18.
- Every BTC trade is single-TP, because of the 0.001 step and $100 minimum.
- The EMA benchmark made only 9 trades: its 2×ATR1D stops usually exceed `max_stop_pct` (6%). Decide at M4 whether the benchmark should get its own stop cap; the spec says "identical sizing".

### M5 live adapters and shadow (2026-10-09; built ahead of the M4 verdict at the user's request)
Plugin (`plugins/binance-futures`):
- `trading.py` (`FuturesTrading`):
  - algo orders through `/fapi/v1/algoOrder`: place, query, cancel, list;
  - account setup: position mode, marginType, leverage, leverage brackets;
  - market orders that ask for `newOrderRespType=RESULT`;
  - reads: positions, userTrades, income;
  - public data: klines (`startTime`/`endTime`), mark klines, funding history, premiumIndex.
- Every reply's outcome is `ok`, `rejected`, `unknown` or `not_sent`.
- `user_stream` subscribes to `ALGO_UPDATE` as well.
- Reached via `EXECUTION_FACTORY.trading()` and `.user_stream()`. `FuturesTrading` and `UserStream` are added to the boundary checker's forbidden-constructor list.

zone-trend:
- `persistence/` (the spec's SQLite WAL schema).
- `live/oms.py`, `reconciler.py`, `venues.py` (paper for shadow, OMS for live), `runner.py`, `notify.py`, `settings.py`, `main.py`.
- `risk/decide.py`: the one accept/reject function the engine and the runner share. The engine refactor reproduced the BTC+ETH run exactly.
- CLI: `zt shadow`, `zt live [--resume]`, `zt kill`, `zt parity`.

Decisions and findings:
- **Replacement stops alternate** between closePosition and a reduce-only quantity stop. Binance allows one closePosition stop per direction (-4130), and place-then-cancel needs two stops briefly.
- **Open quantity comes from fills only**, deduplicated by trade id across the stream, REST responses and reconciler backfill.
- **Closing order:** exits close at market first and cancel protection only once flat. The kill switch keeps a symbol's stop while Binance still shows a position on it.
- **The reconciler touches only `zt-` orders** and the configured symbols. A foreign position gets a warning, once, and nothing else.
- **Shadow** runs the backtest `Broker` on live 1m last/mark klines and funding from public mainnet REST, so shadow fills follow the backtest's fill model. Paper positions are not persisted across restarts; signals are.
- **Live equity** = `ZT_CAPITAL` + realized + open PnL of zone-trend positions only, so it works on a sub-account or a shared account.
- **Live mode** needs `ZT_LIVE_ACK=real-money-zone-trend`, `BINANCE_FUTURES_ENV=mainnet` and the trading key. It refuses to start while halted unless given `--resume`.
- **Stages:** micro (BTC/ETH, 1 position, minimum size, single TP), half (top 5, 2 positions, ×0.5), full.
- **Warm-up must replay from listing.** The core's state is path-dependent with long memory. Fixed warm-ups disagree with a from-listing replay and don't converge: ETH zone agreement was 61% at 420d, 100% at 800d and 66% at 1100d. Warm-up now pages REST history from each symbol's first bar (~30 s per symbol). It matches the Vision replay's zones exactly for BTC and ETH.
- **Data sanity** = mark vs the hour's close. Bars come from REST at each close, so the spec's stale-stream rule does not apply.

Tests:
- OMS scenarios against an in-memory exchange (`tests/live/fake_exchange.py`):
  - entry, stop, targets;
  - stop-out cleanup;
  - TP1 → breakeven with place-then-cancel;
  - alternating trailing stops;
  - ambiguous entry, placed or absent;
  - rejected entry;
  - stop failing twice → flatten;
  - -2021 → close;
  - a failed close keeps the stop;
  - time exit.
- Reconciler: missing stop, missed fills, unknown entry, unknown and foreign positions, orphan orders. Also the kill switch.
- Runner/backtest parity: the shadow runner's hourly `step` records exactly the replay's signal ids.
- Shadow startup smoke-tested read-only against public mainnet.

Not built (M6): the metrics/health endpoint, heartbeat, daily and weekly reports, systemd units, deploy notes, stage-gate automation, and the weekly displacement refresh (live uses k_sym = 1).

### Prototype: 15m entries (2026-10-09; at the user's request, backtest only)
`timeframes.entry` now accepts `1h` or `15m` (`TF.M15`, `swings.m15`). The core, tape, engine and broker step on entry bars; bar-count windows scale x4 in `config/proto-15m.yaml` (retest 192, limit 12, time stop 288; the FSM cooldown scales in code to 96). ATR-relative parameters transfer as-is. Live/shadow stay on 1H; the funding blackout is still not enforced for market entries (a spec question for 15m, where fills can land inside the window).

Data: `zt data resample-15m` derives 15m months from synced 1m months. Cross-checked against native Vision 15m for 2021-05: OHLC bit-exact on all 2976 quarters; volume differs on exactly one quarter (2021-05-15, the known 1m volume-loss day, quarantined).

Result, BTC+ETH 2021-01..2026-10 (`/research`):
- 1H (`runs/6fccf633e44f`): 43 trades, PF 1.23, +0.03R/trade. Bit-identical to the pre-refactor run (trades/fills/equity/signals all equal), so the refactor changed nothing.
- 15m (`runs/a513731dd6ee`): 92 trades, PF 0.80, −0.09R/trade, DD 5.1%. The loss is all BTC (−0.26R over 51 trades); ETH is flat to slightly better (+0.12R vs +0.10R). Costs rise to 14% of gross wins (from 9%) and min-notional rejections triple.

Verdict: the 1H-tuned entry logic does not transfer to 15m by time-scaling alone. Retuning (or per-symbol entry TFs) would be its own research loop, re-gated by M4.

### Majors backtest, 1H (2026-10-09; at the user's request)
10 large-cap perps (BTC ETH BNB XRP DOGE ADA LINK LTC BCH TRX), 2021-01..2026-10, default config: `runs/713db5ab894f`, 181 trades, PF 0.90, −0.13R/trade, DD 6.9%. Winners: DOGE (+10.9), ADA (+9.8), ETH (+7.0), XRP (+3.0). Losers: LTC (−24.4), BCH (−9.9), LINK (−8.8), TRX, BNB; BTC flat. Portfolio contention costs BTC 6 fills vs the 2-symbol run (17 vs 23). Breadth alone does not fix the edge: per-symbol expectancy ranges from −0.60R (LTC) to +0.44R (DOGE), so symbol selection/weighting is the next question, still behind the M4 gate.

### Majors backtest II, 1H, 18 symbols (2026-10-09; at the user's request)
All 18 large-cap USDT perps listed before 2021 (the prior 10 plus SOL AVAX DOT NEAR XLM ETC FIL ATOM), 2021-01..2026-10, default config. New symbols synced from Vision (4,290 month files, 0 failures; snapshot `1e320f47c3b1f9c9`).
- Zone (`runs/618ce3b2edd7`): 308 trades, PF 0.87, −0.12R/trade, net −48.2 (fees 24.7), DD 14.8%, Sharpe −0.44, end equity 451.8 from $500. Exits: 184 stop, 62 stop-after-TP1, 42 TP2, 16 TP1, 4 time. Top rejections: slope 361, vol_filter 208, no_room 175, stop_too_wide_pct 119, btc_gate 49. No same-minute conflicts; 8 breaker events, not halted.
- Winners: DOT (+14.5, +0.27R, 26 trades), NEAR (+11.9, +0.36R), ADA (+9.1), ETH (+7.7), DOGE (+6.2), FIL (+1.1). Losers: LTC (−25.7, −0.80R), ATOM (−14.5), LINK (−13.9), XLM (−10.3), BCH (−8.0), BNB, AVAX (−0.55R on 9 trades), TRX, SOL, ETC; XRP and BTC flat.
- Benchmark (`runs/351d4cb52ff3`, EMA 20/100): 42 trades, PF 1.03, +0.39R/trade, end 505.8. It beats the zone strategy on this universe, same caveat as before (its 2×ATR1D stops usually exceed `max_stop_pct`, so it trades rarely).
- 2× slippage (`runs/352f5de9c890`): 307 trades, PF 0.81, −0.17R/trade, DD 18.5%, end 425.9. Costs bite: the edge degrades roughly linearly with slippage.
- Verdict: same as Majors I, with more evidence. Breadth adds trades (181 → 308) but not edge (PF 0.90 → 0.87, DD 6.9% → 14.8%). Expectancy still spans −0.80R (LTC) to +0.36R (NEAR), so the strategy is symbol-sensitive; DOT/NEAR/ADA/ETH/DOGE carry the book while LTC/ATOM/LINK/XLM drag it. Symbol selection/weighting remains the open question, still behind the M4 gate.

### Winners-only rerun, 1H (2026-10-09; at the user's request)
The 6 net-positive coins from Majors II (DOT NEAR ADA ETH DOGE FIL), same window and config: `runs/24e2edbf7b0b`, 103 trades, PF 1.47, +0.21R/trade, win rate 50.5%, net +59.9 (fees 9.8), DD 4.2%, Sharpe 0.74, end equity 559.9. Per coin: DOT +19.3 (+0.30R, 26 trades), DOGE +13.0, NEAR +12.5, ADA +9.6, ETH +7.1; FIL flipped to −1.7 (−0.05R, same 13 trades — the equity path, hence sizing, differs without the other symbols). No breaker events. **Selection-bias warning:** these coins were picked for being profitable in this same window, so this run measures concentration, not an out-of-sample edge; the M4 walk-forward stays the gate.
