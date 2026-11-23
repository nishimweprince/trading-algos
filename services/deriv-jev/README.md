# deriv-jev

Walk-forward Deriv DSI strategies with Jev (TypeSafe System One) as the take/skip gate,
plus a zero-spread 70%+ win-rate paper group. Runs in **SHADOW** mode: live M1 bars from
cTrader, live Jev decisions, fills simulated at the next M1 open plus the measured spread
(identical to the backtest). No orders are sent.

Standalone: not a uv workspace member (it is in the root `exclude` list) and keeps its
own `.venv`. Stdlib plus numpy and protobuf.

## Layout

- `service.py`: runner (7-day replay, then polls M1 every minute), SQLite writer, kill switch
- `engine.py`: bar resampling, ATR, per-strategy `Track` (stop / target / trail / time exit)
- `rules.py`: signal rules (momentum, breakout, RSI, EMA)
- `jev.py`: Jev state builders (`state_text`, `state_v1` with last-10-trade history) and `ask_median`
- `ui.py`: stdlib HTTP UI on 127.0.0.1:8091 (Jev group, 70%+ group, backtest section)
- `ct.py` + `ta_plugin_ctrader/_generated`: read-only cTrader Open API client (vendored protobufs)
- `config.json`: strategies, thresholds (Jev history v1, latest fold), `jev_asks` = 3
- `backtest.json`: out-of-sample Jev v1 vs ungated stats shown in the UI

Strategy tags: `JEV-DSI30-M1-mom16-q20-trend`, `JEV-DSI30-M15-mom4-q15-trend`,
`JEV-DSI30-M5-mom16-q20-trend`, `JEV-DSI10-M1-rsi20-trend`, `JEV-DSI10-M5-mom4-q15-trend`,
plus the `wr70` group.

## Run

```bash
cd services/deriv-jev
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in
mkdir -p data logs
.venv/bin/python -u service.py
```

systemd unit: `deploy/deriv-jev.service` (edit User and paths).

- Kill switch: `touch data/KILL` stops new entries.
- Data: `data/deriv_jev.db` (tables `signals`, `trades`); ignored by git.

## Notes

- The Deriv cTrader DSI symbols are CLOSE_ONLY (cTrader synthetics retired 2026-11-19), hence
  shadow mode. The MT5 symbols are unaffected; an MT5 order adapter is the next step.
- Jev answers are nondeterministic; the live gate uses the median of 3 asks.
