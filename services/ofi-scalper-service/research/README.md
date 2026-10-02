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

## Next (Stage 2)

- `features.py`: replay recordings → `GridClock` → `MarketState` → parquet.
- `labels.py`, `train.py`, `backtest_hft.py`, `gates.py` per the plan.
- Confirm hftbacktest's Binance-futures converter reads the recorder's line
  format, or write a converter.
