# Research (Stage 2, not started)

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

## Next (Stage 2)

- `labels.py`, `train.py`, `backtest_hft.py`, `gates.py` per the plan.
- Confirm hftbacktest's Binance-futures converter reads the recorder's line
  format, or write a converter.
