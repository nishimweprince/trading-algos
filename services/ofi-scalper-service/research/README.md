# Research (Stage 2)

Offline only. Install the extra: `uv sync --package ofi-scalper-service --extra research`.

Rules carried from the plan: features come from
`ofi_scalper_service.state_engine` (imported, never re-implemented), driven
through `GridClock` exactly as the live runtime drives it; labels are
triple-barrier on mid; purged, embargoed walk-forward CV by month; final
months held out untouched; the harness writes `gates.json` and the strategy
author never decides pass/fail.

## What history exists (checked 2026-10-01)

`python -m research.collect.vision coverage`:

| data.binance.vision (UM futures) | BTCUSDT range | Usable for |
|---|---|---|
| `aggTrades` daily | 2019-12-31 → yesterday | trade-flow imbalance, VWAP, realized vol, trend |
| `bookTicker` daily | 2023-05-16 → **2024-03-30** | best-level OFI, imbalance, microprice, but only for that window |
| `bookDepth` daily | 2023-01-01 → yesterday | coarse only: cumulative depth at ±1–5 % bands, not touch-level |
| `metrics` daily, `fundingRate` monthly | long | context features |

**Consequence:** for any period after March 2024, best-level and multi-level
book features exist only in our own recordings (`data/raw/`). Research on
recent data needs weeks of recording first; a walk-forward-by-month design
needs several months. The alternative is Tardis.dev (paid), which was declined
for now.

## Replay (built)

`research/replay.py` turns recordings into the exact feature rows the live
service produced. It re-implements nothing: each recorded line goes through
the plugin's live consumer path (`FuturesStreams.replay_line`: depth sync,
snapshots, checkpoints, resets) and each event through
`ScalperRuntime.handle` (grid clock, state engine, burst samples).

```sh
uv sync --python 3.12 --package ofi-scalper-service --extra research   # parquet output
cd services/ofi-scalper-service
python -m research.replay features --profile dev --date 2026-10-02     # -> research/data/features/dev/
python -m research.replay compare  --profile dev --hour 2026-10-02T13  # needs OFI_SAMPLE_LOG_DIR
```

What makes it exact:

- **Control lines in the recording:** `_control@session` at every process
  start (tick sizes, book mode, subscriptions; replay drops all state there,
  as the process had none), `_control@reset` when books were discarded, and
  `<s>@bookCheckpoint` (the full verified book) at the top of every UTC hour in
  diff mode, so any hour can be replayed without the session's REST snapshot.
- **Same sample times:** both anchor the 100 ms grid at the session line, and
  both sample due grid times *before* the triggering update touches the book
  (`FuturesStreams.before_apply`), so book-reading features never see an
  update received at or after the sample time.

Verified 2026-10-02 on real Binance data (Mac, partial mode): 2,007 of 2,007
live samples reproduced, 0 mismatched values. **Still to verify on the VM in
diff mode across an hour boundary** (exercises checkpoints): set
`OFI_SAMPLE_LOG_DIR` for two hours, then `compare --hour <second hour>
--warmup-hours 1`, and check `checkpoints.mismatches == 0`.

Recordings made before 2026-10-02 have no control lines: they replay only
from a session start (where the REST snapshot is), with tick sizes and book
mode taken from the profile.

## Stage 2 pipeline

```sh
uv sync --python 3.12 --package ofi-scalper-service --extra research --extra model
cd services/ofi-scalper-service
ofi-latency --profile dev --seconds 600                     # research/latency.json (feed latency)
python -m research.pipeline --profile dev --provisional --jobs 3 [--order-rtt-ms 40]
```

On the VM, set `OFI_RESEARCH_DIR=/data/ofi/research` so features, scores and
candidates land on the data disk. Keep `--jobs` below the vCPU count so the live
service keeps headroom. The order round trip comes from testnet orders (the
gateway's ledger, or the trades' `rtt_ms`); until one is measured the REST
round trip stands in.

| Step | Module | What |
|---|---|---|
| Eligible days | `pipeline.py` | `ofi-daily-check` clean (no missing hours, no unrecovered breaks), recorded on `OFI_HOST_TAG`, diff mode. Excluded days are printed |
| Split | `cv.py` | Weekly (provisional) or monthly (binding) periods: the walk-forward periods, then 1 held-out period. **Fixed per run name once written** (`splits/<run>.json`) |
| Features | `replay.py` | Any day without a feature file is replayed through the live code |
| Labels | `dataset.py` | Triple barrier on mid at 1/5/10/30 s, barrier `--barrier-bp` (6 = 4 bp maker round trip + 2 bp buffer). Grid samples only. A gap or book reset before a touch: no label |
| Train | `train.py` | Per horizon: CatBoost per expanding fold (purged + 5 min embargo), multi-level OFI PCA fit on each fold's training rows only, isotonic calibration (own PAV) on the out-of-fold predictions, Brier score and reliability per class, a final model on every walk-forward period, SHAP |
| Scores | `scores.py` | Out-of-fold tables for validation days; final-model tables for held-out days only |
| Select | `selection.py` | Horizon x threshold by **mean daily net P&L after costs**, on validation days, out-of-fold scores, pessimistic queue, 1x latency, >= 30 trades |
| Gates | `gates.py` | The chosen policy on the **held-out** period: base (queue model), pessimistic queue, 2x latency, and the most volatile validation days. Writes `models/<version>/` with `gates.json` (every gate, value and threshold), plus `strategy.md` on a pass or `REPORT.md` on a fail |

**The backtest is the live code.** `backtest.py` replays recordings through
`FuturesStreams.replay_line` and `ScalperRuntime.handle` into an
`ExecutionBridge` in shadow mode, so the policy, risk checks, rate governor,
sizing, fees and trade records are the ones that trade live. Only the venue is
simulated (`ShadowVenue`):

- **Queue models:** `pessimistic` fills a resting order only on a trade printed
  through its price; `queue` (risk-averse queue position) joins behind the
  displayed quantity at its level, advances with trades at its price, moves up
  when the level shrinks, and fills partially from the excess.
- **Latency:** an order or cancel reaches the venue `delay` after the
  decision: 1x is the one-way order latency (the recordings already carry the
  real feed latency); 2x adds another feed latency and doubles the order's. A
  post-only order that would cross at arrival is rejected; a cancel can lose to
  a fill.
- **Clocks:** risk (the UTC day for the daily-loss limit) and the order-rate
  governor run on event time. A daily-loss halt is acknowledged the next day,
  standing in for the operator.

hftbacktest was the original plan; it would need `policy.py` rewritten as
numba code, a second implementation the goal prompt rules out.

**Holdout discipline:** `gates.py` records each evaluation in
`holdout_ledger.json` before it runs, and refuses a second one on the same
held-out period. To try again, record more data and start a new run name.

| Run | Data | Outcome |
|---|---|---|
| `--provisional` | >= 3 walk-forward weeks + 1 held-out week | `binding: false`; if it passes, shadow and testnet may load the model |
| `--binding` | >= 3 walk-forward months + 1 held-out month | `binding: true`; required (with approval) before mainnet |

Backtest one candidate by hand:

```sh
python -m research.backtest --profile dev --candidate research/data/candidates/provisional-1/h5 \
    --from 2026-10-12 --to 2026-10-25 --scores oof --threshold 0.55 --queue queue --jobs 3
```
