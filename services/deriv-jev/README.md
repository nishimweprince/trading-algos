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

## MT5 runner (70%+ win-rate group on Deriv MT5)

`mt5_runner.py` trades the `wr70` strategies on a Deriv MT5 account through the RPyC bridge to the
Windows `MetaTrader5` package (Wine, `127.0.0.1:18812`). Signals are the same as the backtest
(`engine.resample`, `engine.atr`, `rules.generic_rules` on completed tf bars).

- Stops and targets are widened to the broker minimum (`trade_stops_level` + spread + 20%) when the
  strategy's own distance is smaller. Every widening is written to the order's `note`.
- Size: `RISK_PCT` (5%) of equity per trade, compounding; one position per strategy (magic 707001-707009).
- Time exit after `max_hold` + 1 minutes.
- `MT5_MODE=shadow` (default): `order_check` + virtual positions on a virtual `SHADOW_EQUITY` ($100)
  account. `MT5_MODE=live`: real orders. Set in `.env.mt5` (see `.env.mt5.example`), then
  `systemctl restart mt5-runner`.
- Kill switch: `touch data/KILL_MT5`. Report: `.venv/bin/python mt5_report.py`. Log: `logs/mt5_runner.log`.

Note: at Deriv MT5 spreads (DSI10 ~2.6, DSI20 ~3.2, DSI30 ~9.4) these strategies were
not profitable in the cost analysis; the widened stops also change them from what was backtested.

## MT5 runner for the Jev group (`jev_mt5.py`)

Trades the 5 Jev-gated strategies on Deriv MT5: an ungated `engine.Track` per strategy fed by MT5 M1
bars (7-day replay, then live), Jev history-v1 state, median of 3 answers, per-strategy threshold.
Approved signals: market order, stop = max(2 ATR, broker minimum + spread + 20%), no target, trailing
stop at best - 1.5R after +1R, time exit after 481 minutes, size = `JEV_RISK_PCT` (5%) of equity.
Magic 708001-708005. `JEV_MT5_MODE=shadow|live` in `.env.mt5`; Jev key from `.env` (`JEV_API_KEY`).
Kill switch `data/KILL_JEV`; data in `data/jev_mt5.db`; log `logs/jev_mt5.log`; unit `deploy/mt5-jev.service`.
