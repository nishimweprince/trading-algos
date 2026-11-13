# solcarry: Gate 1 research package

Kill-or-continue research for **delta-neutral SOL carry**: a staked SOL long (jitoSOL or
native stake) hedged with an equal short. It compares two hedges:

| Family | Hedge | Income |
| --- | --- | --- |
| **P** (perp carry) | USD-margined perpetual short | staking + funding received by shorts − costs; needs a USD reserve for margin calls |
| **D** (dated basis) | Binance COIN-M inverse quarterly short, held to expiry | basis locked at entry + staking on SOL not posted as margin − costs |

P variants by funding source: `-VEL` Velocity SOL-PERP (the venue; short history), `-BIN`
Binance SOLUSDT (regime proxy only), `-HL` Hyperliquid SOL (tradable, hourly history from
2023-06). `-G` variants are **funding-gated** (see below).

**Gate 1 asks:** is there enough carry, on *total deployed capital* and above a risk-free
hurdle (3-month T-bill), to justify building the full model?

> **Evidence tier: `reconstructed`.** A PASS means *proceed to paper trading*, never *go live*.
> Data only: no keys, no orders.

## Quick start

```bash
cd services/solcarry
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . pytest pytest-cov respx hypothesis ruff mypy pandas-stubs types-PyYAML

# 1. Review config/gate1.yaml (margins! thresholds!), then pre-register:
.venv/bin/solcarry lock

# 2. Everything: fetch -> normalize -> validate -> analyze -> gate -> report
.venv/bin/solcarry run-all

# 3. Look at it
.venv/bin/solcarry serve            # http://127.0.0.1:8765
```

The full download is small (SOL-only: ~70 monthly Binance zips plus a few JSON/CSV
files, tens of MB), so it is fine to run on a laptop. Re-runs only fetch what is new.

`solcarry` is a standalone project (excluded from the uv workspace) with its own `.venv`.

## Commands

```
solcarry fetch [--source all|velocity|binance|hyperliquid|defillama|fred|jupiter|okx] [--start] [--end]
solcarry normalize
solcarry validate
solcarry lock
solcarry analyze [--variant ID ...]
solcarry gate [--unlocked]
solcarry report [--run-id]
solcarry depth                 # one Jupiter snapshot, appends to history (cron-friendly)
solcarry run-all [--unlocked] [--skip-fetch]
solcarry serve [--host] [--port]
```

Global options: `--config path/to/gate1.yaml`, `--data-dir path`, `-v`.

Exit codes: `0` ok, `1` unexpected error, `2` validation failed, `3` lock missing/mismatch.

Each run writes `data/results/<UTC timestamp>-<config hash>/` with the effective
`config.yaml`, `validation.json`, `summaries.json`, `gate.json`, `scorecard.csv`, `perp_gating.csv`,
`funding_diagnostics.csv`, `runups.csv`, `perp_windows.parquet`, `perp_regime.csv`,
`dated_cycles.csv`, `dated_entries.parquet`, `dated_entry_quantiles.csv`, `sizing.csv`,
`depth.csv`, `report.md`, PNG charts and `ui.json` (the dashboard bundle).

### Pre-registration

`solcarry lock` hashes the canonical JSON of `thresholds` + `variants` + `windows.alpha` + `gating`
into `config/gate1.lock`. `gate` and `run-all` refuse to run (exit 3) if the lock is
missing or stale. `--unlocked` runs anyway and stamps every output
**UNLOCKED: thresholds not pre-registered**. Lock *before* you look at results;
changing costs or margins does not invalidate the lock, changing a bar does.

### Building a depth series (cron)

Jupiter depth is a point-in-time snapshot. To build a week of hourly history:

```cron
5 * * * * cd /path/to/services/solcarry && .venv/bin/solcarry depth >> data/depth.log 2>&1
```

The gate uses the most recent `jitoSOL` impact at `depth.target_size` (1,000 tokens).

## Dashboard

`solcarry serve` starts a read-only stdlib HTTP server on `127.0.0.1:8765` over
`data/results`. It shows the gate verdict and lock state, the scorecard, funding (7-day
rolling, both sources), the 30-day excess series of each P variant, COIN-M basis vs
days-to-expiry, run-up quantiles and reserve sizes, D cycles, capital sizing, LST depth,
validation and caveats. Pick any past run from the selector. Chart.js loads from cdnjs.

## Method notes

- **Sign convention:** a positive funding fraction means **the short receives**.
- **Velocity units:** `fundingRate` is USD per SOL per period; the fractional hourly rate
  is `fundingRate / oraclePriceTwap`. `validate` proves the per-period field against
  `diff(cumulativeFundingRateLong)` and aborts if the unit assumption looks wrong.
- **Binance funding** is spread evenly over the `funding_interval_hours` hours *before*
  `calc_time` (interval read per row).
- **Run-ups:** `U_T(t) = max_{t<s≤t+T} high_s / close_t − 1`; windows running past the
  end of data are NaN.
- **P reserve:** `β_P = (1 + mm) · q_α(U_T)`, `k_P = 1 + im + β_P`; returns are on `k_P`.
- **D margin:** an inverse short needs SOL margin `m/C ≥ φ(U) = (U + mm)/(1 + U)`, which
  stays below 1, so a fully SOL-collateralized short cannot be liquidated.
- **Gate α:** variants are scored at the highest configured α (0.99 by default); both αs
  are reported.
- **P windows** need ≥ 90% hour coverage; **D cycles** are scored only once expired (and,
  for D2, with ≥ 90% staking-data coverage).
- **neg_run_days** (P) is the longest stretch the cumulative excess carry spends below
  its previous peak.
- **Convergence check (deviation from spec):** comparing the final *hourly* futures close
  with the hourly index close fails on real data (last-trade noise up to 0.6% in an
  illiquid contract). Binance settles on the 30-minute average index, so validation uses
  1m archives around each expiry: median minute-matched `F/I − 1` over the last 30 traded
  minutes (≤ 0.13% for 8 of 9 contracts, 0.47% for SOLUSD_250926), plus a hard check that
  trading stops at the assumed 08:00 UTC expiry. `validate.convergence_tol` is 0.005; a
  wrong expiry or contract mapping produces an error the size of the basis. Hourly data is
  the fallback when 1m files are missing.
- **Full-history run-ups:** `q_0.99(U_2160)` ≈ 1,090% because spot history starts in
  2020; SOL's 2020–21 10x quarters dominate, pushing `φ_static` to ~92%. Set `run.start`
  to study a later regime (it also clips other sources).
- **D cycles:** the first contract is entered at the first hour with dte ≤ `dte_max`;
  each later contract at the first bar at/after the previous expiry (08:00 UTC).

- **Hyperliquid funding** comes from `POST /info {"type":"fundingHistory"}` (500 records
  per call, paged), cached one JSON file per month. Each print is an hourly payment and
  positive = shorts receive. The first weeks (May 2023) are 8-hourly snapshots with an
  ambiguous period; normalization drops everything before the first 24 consecutive hourly
  prints. Like Velocity, HL funding has a 0.01%/8h interest baseline (10.95%/yr), so
  `share_at_floor` is reported for it too.
- **Funding gate (`*-G`):** each hour, `edge = mean_7d(funding, hours < t) + y − resize − r_f`.
  The hedge turns ON when `edge > +hysteresis` and OFF when `edge < −hysteresis` (starts
  OFF, holds state through missing signal). OFF means both legs are closed and the capital
  earns `r_f` (X = 0). Each switch pays `perp_round_trip/2 + lst_swap` on notional; the
  amortized round trip in the ungated cost is replaced by these explicit switch costs.
  `perp_gating.csv` reports time ON, switches and switch drag.
- **Drift** was considered as a source: `data.api.drift.trade` no longer resolves and the
  public historical bucket is empty (2026-11). Velocity is the Drift v2 fork.

## Known limitations

- Velocity history starts at the mid-2026 relaunch; Velocity-based P variants will be
  `INSUFFICIENT_DATA` until roughly 2027. Binance funding is a regime **proxy** only.
- Staking history (DeFiLlama) starts 2025-03-26; staking windows are clipped to it.
  Current jitoSOL base APY (~4.65%) is well below older back-of-envelope figures.
- Native-stake yield = `y_lst / (1 − pool_fee)`: an approximation.
- `D2-MANAGED` assumes top-ups always land within `T_transfer`: **optimistic**.
- DTB3 is a discount-basis yield, converted as `ln(1 + DTB3/100)`: an approximation.
- Single-call reserve approximation; repeated calls, funding paid during stress and
  correlated LST depeg are Gate 2.
- No venue/counterparty, smart-contract or custody risk modelling.
- Binance REST and Bybit are geo-blocked from some locations: everything Binance comes
  from `data.binance.vision`. The unpublished current month is filled from daily files.
- Venue eligibility for actual trading depends on your jurisdiction.

## Tests

```bash
.venv/bin/pytest --cov=solcarry.analytics --cov=solcarry.gate   # offline
.venv/bin/pytest -m network                                    # live endpoints
.venv/bin/ruff check src tests && .venv/bin/mypy
```
