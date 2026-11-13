# IPDA backtest (XAUUSD M1)

Replays the live reversal entry (`src/strategy.py`, `src/service.py`) on HistData gold
1-minute bid data and renders a static performance report.

```bash
cd ipda/backtest
uv run --no-sync python parity.py          # vectorised signals == live service code, minute by minute
uv run --no-sync python run_backtest.py --open   # ~15 s; writes results/results.json + results/report.html
uv run --no-sync python run_backtest.py --render-only --open   # re-render after editing ui/
uv run --no-sync python run_ipda.py --open       # ~45 s; the ICT IPDA model -> results/ipda_report.html
```

| File | Role |
| --- | --- |
| `data.py` | Loads `DAT_ASCII_XAUUSD_M1_*.csv`, localises **America/New_York** (the gold files follow NY DST, not fixed EST), caches parquet in `.cache/` |
| `engine.py` | 3M buckets, forming-bar Wilder RSI cross, SignalGate lock, session gate, SL/TP walk on 1M bars |
| `run_ipda.py` | Streams every minute through `src/ipda_model.py` (the live class), simulates limit fills; pre-registered verdict per docs/ipda-model.md |
| `run_backtest.py` | Default FTMO profile + diagnostic variants, IS 2009–2019 / OOS 2020+ |
| `ui/report.template.html` | Self-contained report (no CDN), data embedded at render time |

Fill model: buy at ask = bid + spread (default $0.20), exits on the closing side, a 1M bar
touching both SL and TP counts as SL. Every signal is an independent position (no cap, no flip),
as in the live service.
