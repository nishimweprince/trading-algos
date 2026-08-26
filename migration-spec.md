# Migration spec

Tracking document for the move from 20 flat sibling directories to
`services/` + `packages/`. Update the status boxes as work lands; the
verification command under each item is what "done" means.

- **Branch:** merged to `main` by `8aba09e`
- **Baseline commit:** `1a0dd73`
- **Status:** Phase 3.3 macOS/cTrader cutover complete; `mt5-trader/` retirement deferred
- **Architecture reference:** [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 1. Scope

**In scope this pass:** notification-service, execution-service (merging
`ctrader-markets` + `mt5-trader`), backtesting-service (from `session-hedging`),
the shared `ta-*` packages, and the deployment/CI templates.

**Explicitly out of scope:** every other top-level project. They keep their own
virtualenvs and are not uv workspace members. Verified byte-identical to
`1a0dd73`: `ipda`, `lux-algo`, `fu-strategy`, `vrvp-strategy`, `lookup-trader`,
`telegram-bot`, `signals-scrapper`, `forex-execution`, `pump-fun`,
`bitcoin9to5`, `tinga-tinga`, `jesse-strategies`, `telegram-metatrader`,
`binance-crypto`.

> **Amended during §3.5.** Three of those — `ipda`, `lux-algo` and
> `lookup-trader/server` — are now uv workspace members. Retiring their
> duplicated notifier and logging code means depending on `ta-core` and
> `ta-notify`, and `workspace = true` sources only resolve for members. It also
> gave `lux-algo` an environment it never had: no `.venv` existed for it.
> Adding them was insertion-only in `uv.lock` — no existing member's resolved
> versions moved — but the lock now also carries `lookup-trader`'s analytics
> stack (pandas, scikit-learn, optuna, matplotlib). Per-member installs
> (`uv sync --package …`, which is what CI does) stay lean; only
> `uv sync --all-packages` pays for it. The rest of the list is unchanged, and
> `fu-strategy` in particular is **not** a member — see §3.5.

---

## 2. Completed

| # | Commit | Phase |
|---|---|---|
| 1 | `1ec3b4e` | Workspace skeleton (`services/`, `packages/`, `apps/`, `infra/`) |
| 2 | `816d92e` | Extract `ta-core`, `ta-contracts` |
| 3 | `4cbf3ab` | Extract `ta-store`, `ta-notify`, `ta-clients` |
| 4 | `522bbcc` | `ctrader-markets` → `services/execution-service`, as a package |
| 5 | `395c3d5` | execution-service onto the shared packages |
| 6 | `1752e88` | `mt5-trader` merged in as the `mt5` broker adapter |
| 7 | `8022b82` | `session-hedging` → `services/backtesting-service` (verbatim lift) |
| 8 | `061d4e6` | backtesting-service onto shared packages + strategy seam |
| 9 | `b12fecc` | Service template, ARCHITECTURE.md, consolidated deployment |

### Verified state

| Member | Tests |
|---|---|
| `ta-core` | 19 |
| `ta-contracts` | 41 |
| `ta-store` | 24 |
| `ta-notify` | 14 |
| `ta-clients` | 42 (34 + 8 for `CandleStore`) |
| `execution-service` | 317 |
| `backtesting-service` | 578 (570 baseline + 8 new) |
| **Python total** | **1035** |
| `notification-service` (TS) | 18 |
| `mt5-trader` (frozen) | 101 |

Lint and format clean on every member. Docs site builds (128 routes; 125 indexed content pages).

**Test-count provenance** — the numbers that prove nothing was lost:
`ctrader-markets` was 238 before the move and execution-service's cTrader tests
are still 238. `session-hedging` was 570 passed / 5 skipped and
backtesting-service is 570 / 5 plus 8 new registry tests.

`mt5-trader` was 101, accounted for as 67 + 22 + 12:

- **67** moved wholesale into `services/execution-service/tests/mt5/`
  (`test_service`, `test_market_data_service`, `test_notification_client`,
  `test_api`)
- **22** — `test_models.py`, parametrised — moved to
  `packages/ta-contracts/tests/test_signal_models.py`
- **12** (`test_config` 7, `test_main` 2, `test_logging_config` 3) tested config,
  argparse and logging that `ta-core` / `ta-notify` now own and already cover.
  The parts unique to MT5 were rewritten as the 8 tests in
  `tests/mt5/test_mt5_config.py`, which is where the two missing validators in
  §6 were caught.

### Determinism gate

Four backtests over the committed XAUUSD candles, 606 trades total. These
hashes held through the move, through four shared-package swaps, and through
two `ruff format` passes.

The gate is now enforced rather than eyeballed. Three things were wrong with
it, all fixed before Stage B started:

- **It could not fail.** It printed the status line and `continue`d on any
  non-200, and contained no `sys.exit`. A run where every case 404'd exited 0.
  It now compares against `scripts/determinism_baseline.json` and exits 1 on any
  mismatch, missing case or non-200.
- **Its candles were not committed**, despite the sentence above saying they
  were: `services/backtesting-service/.gitignore` ignored `data/`. The M15 and
  H1 files the four cases read are now tracked (~3.9 MB); M1/H4/D1 stay ignored.
- **It read the developer's own gitignored `.env`**, so the four hashes were
  reproducible on exactly one machine. It now loads
  `scripts/determinism.env`, committed and credential-scrubbed. All four hashes
  are unchanged by the switch.

It runs in CI as the `determinism` job of `backtesting-service-ci.yml`. A
deliberate behaviour change regenerates the baseline via `--update-baseline`,
committed alongside the change with the cause recorded here. **An unexplained
hash change is a regression, not a baseline update.**

```
hedge_pair_M15      f47a65c03005cfd6eb7c83413ccdb06132267f40462bd66ab69382d02929547a
synthetic_breakout  3d44d78e5d00f3be4105e05ec72bd7a096a735ae64fbb2bb120af9be638c7a0a
rr2_M15             211fb860f77c8771067cb2db766d1bfff52077da8a182892e7bc0d3329500901
H1                  b45d08459d0e2272f54e8fabb71b13ca5e574ee0d81a57311ab875e279e6ed87
```

```bash
uv run --package backtesting-service python services/backtesting-service/scripts/determinism_gate.py services/backtesting-service package
```

---

## 3. Outstanding

### 3.1 Merge the branch — **blocking everything below**

- [x] Review (reviews best phase-by-phase; each commit is independently revertable)
- [x] Merge to `main`

### 3.2 Live smoke — **gates the cutover**

Needs broker credentials and the Windows terminal host. Cannot be done from a
dev machine.

- [x] **macOS / cTrader, port 8010.** `ADAPTERS=ctrader`.
      `/health/trading-ready` returns ready; `/v1/market-data/tick` returns a
      real quote.
- [x] **Windows / MT5, port 8000.** `ADAPTERS=mt5`, `DATABASE_PATH` pointed at
      the **existing** `signals.db`. `/health/ready` returns ready.
- [x] **Idempotency against live data.** Submit one real signal; confirm it
      appears in `signals.db` exactly once. Re-POST the same `signal_id`;
      confirm the stored result is replayed and **no second order is placed.**
      This is the single highest-risk check in the whole migration.
- [x] **Shadow diff.** Run backtesting-service with
      `MARKET_EXECUTION_MODE=shadow` against the new gateway; diff staged
      payloads against ones recorded pre-migration.

Evidence is recorded in [infra/cutover-evidence.md](infra/cutover-evidence.md). The macOS checks
were independently re-run during cutover; the Windows, MT5 idempotency and shadow checks are
operator-attested because their host-local artifacts are not accessible from this checkout.

### 3.3 Cutover

Only after 3.2. Full steps in [mt5-trader/FROZEN.md](mt5-trader/FROZEN.md).

- [x] `uv sync --all-packages` on each host (the binary is now the workspace
      console script, not a per-project venv)
- [x] launchd: bootout `com.ctrader-markets.*`, bootstrap
      `com.execution-service.*` — **in one sitting**, since both bind the same
      port
- [x] Watch one full trading session on the new service — **waived by operator** after the recorded
      manual smoke, idempotency and shadow attestations; the prepared session and verification
      scripts were not re-run as a cutover gate. See
      [infra/cutover-evidence.md](infra/cutover-evidence.md).

#### Deferred

- [ ] Delete `mt5-trader/` — deferred to a later MT5-retirement goal under
      [mt5-trader/FROZEN.md](mt5-trader/FROZEN.md), not blocked on smoke. The directory and its CI
      workflow remain intact.

### 3.4 Stage B — split `engine.py` behind the strategy seam

The largest remaining piece, and genuinely multi-session. `engine.py` is 3,816
lines with entry, OCO and prop-guard paths interleaved. The seam
(`registry.py`, `strategy` on `BacktestRequest`) exists; the engine does not yet
run through it.

**Run the determinism gate after every step.** That is what makes this a
checkable operation rather than a hopeful one.

- [x] Extract `harness/` — `fills`, `costs`, `sizing`, `metrics`, `units`,
      `validation`. Already clean modules; lowest risk, do first. `harness/`
      deliberately re-exports nothing: importers name the module, which keeps
      the one-way `engine → harness → models` dependency visible and keeps
      `models._valid_cost_surface`'s lazy import from closing a cycle.
- [x] Extract `data/` — candle store + JSONL cache. `CandleStore` now lives in
      `ta-clients`, taking its settings through a `SupportsCandleStore` Protocol
      the way `execution.py` already did, since backtesting-service's 465-line
      `Settings` could not come with it. `TIMEFRAME_MINUTES` moved to
      `ta-contracts` beside `Timeframe`. The gateway response is parsed with
      `ta_contracts.CandlesResponse`; backtesting-service's same-named model
      carries an extra `source` field for its own API and stayed behind.
- [ ] Extract `research/` — S1–S9, walk-forward, monte carlo, reporting.
- [ ] Split `engine.py` last, moving hedge-pair logic behind
      `StrategyPlugin.build`.

### 3.5 Retire the remaining duplication

`ta-notify` and `ta-core` now exist, so these are small. Each one retires a copy
that can silently drift. Recommended **before** 3.4 — low risk, and it will
surface anything awkward in the `ta-*` APIs while Stage B is still on paper.

| Project | Duplicated | → |
|---|---|---|
| `ipda` | `src/notifier.py`, `src/logging_config.py` | `ta-notify`, `ta-core` |
| `lookup-trader` | `server/app/services/meta_event_notifications.py` | `ta-notify` |
| `fu-strategy` | `app/api/notifications.py` | `ta-notify` |
| `lux-algo` | `src/lux_algo/logging_config.py` | `ta-core` |

- [ ] ipda
- [ ] lookup-trader
- [ ] fu-strategy
- [ ] lux-algo

### 3.6 Documentation loose ends

- [x] `services/backtesting-service/README.md` — replace stale `session-hedging` paths and commands
      with the workspace service paths and console script
- [x] Docs site: add `execution-service` and `backtesting-service` page trees and redirect the
      legacy `ctrader-markets` / `mt5-trader` routes to the unified execution-service docs
- [ ] Docs site config pages under `apps/docs/app/{ipda,lux-algo,signals-scrapper}/`
      document `MT5_SIGNAL_API_URL` — still correct today, revisit when 3.5 lands

---

## 4. Contracts that must not be "tidied"

These look like inconsistencies and are load-bearing. Anyone touching the
shared packages should read this section first.

| Contract | Where | Why |
|---|---|---|
| `Notifier.send` **never raises** | `ta-notify` | A notification failure must not propagate into a trading path |
| Execution client returns `UNKNOWN`, **never** `REJECTED`, on transport failure | `ta-clients` | The order may have reached the broker; the caller reconciles rather than resubmits. Deliberately the opposite of the rule above |
| `OPERATION_NAMESPACE` is frozen | `ta-clients` | Operation ids are `uuid5`-derived from it; changing it makes in-flight operations look new and opens a second position where a retry was intended |
| `SignalRequest.canonical_json` is frozen | `ta-contracts` | Feeds the replay hash checked against the live `signals.db`; changing it re-executes already-filled signals |
| `Candle` ≠ `LegacyCandle` | `ta-contracts` | mt5's `/v1/candles` returns epoch-int `time` and int `volume` with no provenance. A real contract difference, not drift |
| `ALLOWED_SYMBOLS` preserves case | `execution-service` | MT5 lookup is case-sensitive; Deriv names are `Volatility 75 Index` |

---

## 5. Decisions and deviations

Recorded so they are not re-litigated.

**npm workspaces was attempted and reverted.** notification-service needs
`zod@^3.24.0`, nextra needs `zod@^4.1.12`. Hoisting put zod 3 at the root and
gave `nextra` and `nextra-theme-docs` *separate* nested zod 4 copies, so nextra
built a schema in one instance and validated it in the other — every MDX page
failed to prerender with `Invalid input: expected nonoptional, received
undefined → at children`. The root `package.json` is a task runner instead; the
two projects keep their own lockfiles. See
[NODE_WORKSPACES.md](NODE_WORKSPACES.md).

**backtesting-service does not inherit `ta_core.BaseServiceSettings.`** The base
makes `API_KEY` required with a 16-character minimum, which is right for a
service that reaches a broker and wrong for a research surface routinely run
locally with no key on `0.0.0.0:8012`. Inheriting would turn a supported mode
into a startup failure. The class docstring says so.

**The reusable CI workflow lives in `.github/workflows/`, not `infra/ci/`.**
GitHub only resolves `uses: ./.github/workflows/<file>`.

**`mt5-trader/` was restored rather than deleted.** The plan required the old
service stay runnable until the live smoke passes. It is frozen, untouched, and
still passes its 101 tests.

**launchd labels changed** from `com.ctrader-markets.*` to
`com.execution-service.*`. launchd keys on the label, so the old jobs keep
running under the old name until explicitly booted out.

---

## 6. Defects found during migration

All fixed on the branch. Listed because each one was live before the move.

| Defect | Found by |
|---|---|
| `ALLOWED_SYMBOLS` was being upper-cased, silently breaking every Deriv symbol | mt5-trader's `test_market_data_service` |
| `ALLOWED_SIGNAL_SOURCES` slug validation not carried over | mt5-trader's `test_config` |
| `DEFAULT/MAXIMUM_DEVIATION_POINTS` bounds check not carried over | mt5-trader's `test_config` |
| `s7_artifact.py` walked `parents[2]` to find `reports/` — correct before the package gained a level | backtesting-service's `test_api` |
| Two `OperationState` enum classes coexisting (service-local `models.py` vs `ta_contracts`), so `is` comparisons failed | execution-service's `test_execution_service` |
| `ta-core` `/health/live` returned `"alive"`; both real services return `"ok"` | mt5-trader's `test_api` |

**Latent, unrelated to this migration but found in passing:** `next@15.5.24`
breaks the nextra MDX build outright — every page fails to prerender. The
pinned `apps/docs/package-lock.json` is the only thing holding it. It will bite
on the next `npm update`. Worth its own ticket.

---

## 7. Rollback

Every phase is a separate commit and `git mv` preserved history, so any phase
reverts cleanly. Before the cutover in 3.3, rollback is free: `mt5-trader/` and
the old launchd labels are both still live. After the cutover, roll back by
re-bootstrapping the old plists and pointing `DATABASE_PATH` back — the schema
is unchanged and the replay hash is byte-identical, so no data migration is
needed in either direction.

---

## 8. Full verification

```bash
# every Python workspace member
for p in ta-core ta-contracts ta-store ta-notify ta-clients execution-service backtesting-service; do
  d=$([ -d "packages/$p" ] && echo packages/$p || echo services/$p)
  (cd "$d" && uv run --package $p ruff check . && uv run --package $p ruff format --check . \
     && uv run --package $p pytest -m "not integration")
done

# TypeScript
npm run notifications:test
npm run docs:build

# backtest determinism
uv run --package backtesting-service python services/backtesting-service/scripts/determinism_gate.py services/backtesting-service package

# frozen service, until 3.3 completes
(cd mt5-trader && ./.venv/bin/python -m pytest -m "not integration")
```
