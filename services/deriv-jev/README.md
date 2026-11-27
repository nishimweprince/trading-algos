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
The trailing stop is applied tick by tick in live mode: between minute steps the runner reads every tick since
the last check (`JEV_TRAIL_POLL`, default 1 s), moves the stop to the latest level, and closes at market if a tick
crossed a level the broker stop had not reached yet.
Magic 708001-708005. `JEV_MT5_MODE=shadow|live` in `.env.mt5`; Jev key from `.env` (`JEV_API_KEY`).
Kill switch `data/KILL_JEV`; data in `data/jev_mt5.db`; log `logs/jev_mt5.log`; unit `deploy/mt5-jev.service`.

## Live MT5 dashboard

`mt5_live_ui.py` serves broker positions, seven days of attributed deals, and WR70/Jev
runner status at `http://127.0.0.1:8092/`. It reads the existing MT5 bridge through
the same virtual environment as the runners. Install `deploy/mt5-live-ui.service`
on the trading host and enable it with `systemctl enable --now mt5-live-ui`.
For a tunnel on the same host, use `http://127.0.0.1:8092` as the origin and protect
the hostname with Cloudflare Access because the dashboard exposes trading activity.

## Paper dashboard and Trek spike-flip test

Extra paper accounts are defined in `paper_variants.json` (read only by the paper trader and its dashboard, via
`paper_cfg.py`). Each clones the 5 former-Jev strategies with overrides, under its own $100 account and tag prefix:
`T3` trail 3R / 24 h limit, `NS` natural stops only (skip signals whose stop would be widened to the broker minimum),
`SP` skip entries whose spread is below the prior-hour median. Backtests are in `research/jev_variants`.
Groups listed under `retired` in `paper_variants.json` take no new entries but keep their history on the dashboard.
Retired on 2026-11-27: `wr70`, `jev_natural` (NS) and `jev_spread` (SP). Active: the former Jev group and `jev_t3`.

`deriv_paper.py` simulates the 14 DSI strategies on live Deriv MT5 bid/ask (no orders). `deriv_paper_ui.py`
serves it at `http://127.0.0.1:8092/`, with a second tab at `/trek`.

`trek_paper.py` (unit `deploy/deriv-trek-paper.service`) is a prospective paper test of the one lead from
`research/dsi_analogs`: on Trek Up and Trek Down, fade an apparent drift flip 120 s after a spike, and hold for 30 s.
The rule and the pass criteria were fixed before the test: from 2026-11-27 to 2027-02-19 UTC (12 weeks, because the rule fires less than once a day per symbol), the pooled mean net
must be above 0 bp with daily t ≥ 2, and both symbols must be positive. Fills are simulated on recorded ticks in
batches of about 20 s. Data in `data/trek.db`, log in `logs/trek_paper.log`, kill switch `data/KILL_TREK`.

`leads_paper.py` (unit `deploy/deriv-leads-paper.service`) paper-tests the two t ≥ 2 walk-forward leads over the same
window, with rules fixed in advance: Crash Boom Flip 1000 run-follow (`run4`, 30 min hold) and Trek Up volatility shock
(`vshock3`, 10 min primary and 30 min secondary). A lead passes if its primary rule's mean net is above 0 bp with
daily t ≥ 2. Tabs `/flip1000` and `/trek-vshock`; data in `data/leads.db`, log in `logs/leads_paper.log`, kill switch
`data/KILL_LEADS`.

The `/breakeven` tab is a read-only view of the live IC Markets runner (`services/icmarkets-m8`): every break-even
action from its log and orders table, the lag from the first tick at 4 ATR in favour to the stop change (once a minute
before 2026-11-26 11:02 UTC, tick-level since), and each trade's result from the broker's deals.

## IC Markets synthetic history

`icmarkets_history.py` runs inside a dedicated MT5 research terminal on
`ICMarketsSC-MT5-3`. It exports only the 12 verified Stochastic, Boom, Crash,
and Jump instruments. Use an investor login where available. The exporter
checks the terminal path and server before requesting history.

After signing into the research terminal, run on the VPS:

```bash
env WINEPREFIX=/opt/icmarkets-research/wine DISPLAY=:98 WINEDEBUG=-all \
  flock -n /opt/icmarkets-research/export.lock \
  wine 'C:\Program Files\Python311\python.exe' \
  'Z:\opt\icmarkets-research\icmarkets_history.py' \
  --terminal 'C:\IC Markets Research\terminal64.exe' \
  --output 'Z:\opt\icmarkets-research\history' \
  --start 2026-09-24 --end 2026-11-24
```

The end date is exclusive. Each successful UTC day is written atomically to a
NumPy archive with a structured `rows` array. Re-running skips existing files
and retries missing days. Use `--kind m1` for a faster candle-only pass or
`--symbols Stochastic_Core` for a single-symbol probe. The per-symbol JSON
contains current contract settings. Check gaps and coverage before using the
exports for a walk-forward replay.
