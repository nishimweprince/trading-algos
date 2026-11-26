# DSI analogs and non-DSI patterns on Deriv synthetics

Research from 26 November 2026. It asks whether the deriv-jev DSI strategies, or any other event pattern,
survive realistic costs on other Deriv synthetic indices and on IC Markets synthetics. The scripts ran on the
Contabo MT5 VPS against the Deriv MT5 bridge (`127.0.0.1:18812`). They read quotes only and never send orders.
Paths are hard-coded to `/opt/dsi-analogs` on that host. Heavy runs belong there, not on a laptop.

## Verdict

Nothing tradable was found.

- **DSI at Deriv MT5 cost.** The spread costs about 0.36–0.44R per round trip. DSI30's spread averages 4.7 in
  quiet periods and 10.0 during strong 15-minute moves. All three DSI30 configs lose. DSI10 M1 `rsi20_trend`
  is +0.15R on ticks (t 1.3), which is not significant.
- **Bar backtests are not trustworthy here.** The M1-bar grid (`bt.py`) showed Crash Boom Flip 150/300/500
  trend rules at t 9–13. On tick replay (`tsim.py`, real bid/ask, 0–2 s latency) they fall to about 0R.
- **Tick walk-forward** (`wf.py`): 120 days of ticks, 22 symbols, 3,346 non-DSI event rules, training on 28 days
  and testing the next 7. No rule recurred, before or after costs. The best t was 3.4, which is about what
  chance produces at this many tests. The V75 control and 528 hour-of-day rules produced 0 passes.
- **IC Markets synthetics** (`icp.py`, 12 symbols): variance ratios are about 1, so they behave like
  memoryless random walks. The tick walk-forward (`ic_prep.py` then `wf.py` with `WF_DIR=/opt/dsi-analogs/wf_ic`;
  85 days, 2,246 rules, shuffled controls `CTRL_BOOM` and `CTRL_STOCH`) selected nothing in any test week. IC spreads
  are 1.3–2.5 bp, against a best before-cost effect of about 1.5 bp.
- **Lower bar, t ≥ 2** (`lowbar.py`; walk-forward with `WF_TMIN=2 WF_OUT=out_t2`): over the full sample, 0 IC rules
  and 1 Deriv rule pass (Trek Down `run6|follow|30`, +0.011 bp). The t ≥ 2 walk-forward loses out of sample, pooled.
  Its two most consistent picks are paper-tested by `../../leads_paper.py`: Flip 1000 `run4|follow|1800`
  (+0.65 bp, 4/5 weeks) and Trek Up `vshock3|follow` (600 s: +0.86 bp; 1800 s: +2.4 bp; 4/4 weeks).
- **One lead:** the Trek Up/Down spike-flip fade (`post120_flip|fade|30`, +0.4 to +0.6 bp, t 3.4 and 2.0). It is
  thin: 105 and 82 trades in 119 days, and only 2 and 1 weeks had the 10 trades needed to count toward weekly
  consistency. It is being tested on new data by `../../trek_paper.py`; see the "Trek spike-flip
  test" tab of the paper dashboard.

## Lesson: run a martingale control through every event pipeline

The first walk-forward run showed a "retest-follow" edge with t 10–16 on 8 symbols. A Gaussian random walk fed
through the same code showed it too. The cause was that retest events were not in time order, and the
one-position-at-a-time skip logic then chose trades using later events, which peeks at future prices.
`wf.py` now sorts every family by time. `diag.py` runs real, shuffled and Gaussian series through the
pipeline and must show about 0 on the simulated ones.

## Scripts, in run order

| Step | Script | What it does |
|---|---|---|
| 1 | `survey.py` | Contract specs, 24 h spread (quiet vs trend), tick rate, variance ratios; saves 69.5 d of M1 bars (the terminal caps at 99,999 bars) |
| 2 | `persist.py` | Variance ratios and autocorrelation with spikes removed |
| 3 | `bt.py` + `dzr.py`, `an.py` | M1-bar grid of the deriv-jev rules plus the drift z-score, in-sample/out-of-sample halves, and its summary |
| 4 | `ticks.py`, `tsim.py` | 28 d of ticks; tick replay of the bar winners at 0/1/2 s latency |
| 5 | `icp.py` | The same structure check on the IC Markets history in `/opt/icmarkets-research/history` |
| 6 | `depth.py`, `pull.py` | Tick history depth per family; 120 d tick pull (`pull.py 120`) |
| 7 | `wf.py`, `wfan.py`, `near.py` | Tick walk-forward per symbol (`wf.py CODE ...`), its summary, and the near-misses |
| 8 | `diag.py` | Controls: real vs shuffled vs Gaussian, split by side, entry-delay sweep |
| 9 | `ic_prep.py` | IC Markets daily tick exports into the `wf.py` format, plus shuffled controls |
| 10 | `lowbar.py` | Full-sample rules at a lower bar (`WF_TMIN`, default 2), with the controls as the false-positive baseline |

`wf.py`, `wfan.py`, `near.py` and `lowbar.py` read `WF_DIR` (data folder), `WF_OUT` (results subfolder, default
`out`) and `WF_TMIN` (walk-forward selection threshold, default 3).

`bt.py`, `tsim.py` and `persist.py` use the numba venv at `/opt/dsi-analogs/.venv`. `survey.py`, `ticks.py`,
`depth.py` and `pull.py` need the bridge, so run them with `services/deriv-jev/.venv`.
