# walkforward-patterns

Runs the **top 20 walk-forward pattern strategies** (XAUUSD, USDJPY, AUDUSD; 5/10/15M chart patterns and three
indicator rules) on a **cTrader demo** through the execution hub, every strategy independently: fixed lots, fixed pip
stop, 2R take profit, 48h market-minute hold, flat on Friday at 16:45 New York.

Source of the strategies: `~/Documents/Markets/Docs/Walk Forward` (SPEC.md, config, reference code and trades),
produced by `~/Documents/Markets/Research/xau-wf-alpha`. **They are not proven.** Most were picked as the best of many
variants after seeing the data; 2026 year-to-date was negative in the backtest. The demo run is the real test.

Owner decisions (2026-11-17): new FTMO $100k cTrader demo (hedging) · 0.10 lots on all 20 · runs on the jito FRA
host next to the execution hub · parity, then straight to demo (no dry-run week) · FTMO daily/total loss limits are
**recorded only, never enforced** · kill rule: a strategy is disabled after ≥ 60 trades with cumulative R < 0.

## Differences from the SPEC (the code follows the data)

1. **Research clock = America/New_York with DST, not fixed UTC−5.** The HistData files (all three symbols) are New
   York wall-clock time: gold reopens at 18:00 and FX at 17:00 on the label in every month, summer and winter. Live
   UTC timestamps are converted with the `America/New_York` zone (`clock.py`). So the Friday flat is 20:45 UTC in
   summer and 21:45 UTC in winter, the no-entry window is 20:00–22:59 / 21:00–23:59 UTC, and the 4H buckets start at
   00/04/…/20 New York time. The `*_utc` columns in `reference/trades/*.csv` are 1h off in summer; ignore them.
2. The shipped `hs.py` lacks the `trend4` column that `top20.py` filters S07 on; it is the standard 4H EMA50 trend
   (as in `hs4_placebo.py`), and that is what the engine uses. S07 parity is exact.
3. "Same trading day" entry gating applies only to the rule strategies (S10/S18/S19, `lib.entry_index`); the pattern
   engines check only the no-entry window and the 30-minute limit, as their research code does.
4. The `mode` (scalp2h/intraday8h) in `top20.py`'s setup tuples does not change the 2R signal set and is ignored.

## How it works

```
engine.Market (one per symbol)          M1 bid -> 5/10/15M, 1H, 4H, trading-day bars (research clock)
  indicators.py                         EMA / Wilder ATR / RSI / ADX = pandas ewm(adjust=False), value for value
  patterns.PatternDetector / HsDetector incremental ports of patterns.detect_all / hs.detect (same loop body)
  Rule15 + RuleContext                  S10/S18/S19 first-bar-true rules
  _qualify / _resolve                   trend filter, entry gating, risk qualification, confluence (S08/S20), one position
exits.TimeExit / SimPosition            48h market-minute stop + Friday flat; research fill rules for replay
replay.py                               npz history -> trades CSVs + parity table
live.Runner                             hub candles -> engine -> hub orders; deals -> exits; kill rule; FTMO log
hub.py                                  execution-service client (quote, candles, positions, deals, orders, close)
journal.py / status.py / cli.py         SQLite journal, read-only status page, `wfp` CLI
```

Replay and live run the **same** `Market` object. Live, at each minute boundary T: feed the closed M1 bars, sync broker
closes, apply time exits, `signal_now(T)` (close buckets ending at T, resolve signals with entry minute T, qualified
against the current bid), send market orders with SL/TP distances; label = strategy `source` (`wf_s01`…`wf_s20`).
Broker SL/TP exits are read back from the hub's deal history (real exit price, commission, swap).

## Verification

```bash
cd services/walkforward-patterns
D=~/Documents/Markets/Docs/Walk\ Forward/reference
uv run --no-sync pytest                                         # unit tests (no data needed)
WF_DATA="$D/data" WF_REFERENCE="$D/trades" uv run --no-sync pytest   # + parity and the live path
uv run --no-sync wfp-replay --data "$D/data" --out /tmp/wf --reference "$D/trades"
```

* Indicators and bars equal `lib.py` to 1e-9; the incremental detectors emit exactly the batch signals (synthetic and
  2023 gold, k=3/5, 5M/15M).
* **Replay parity: 20/20 pass.** 19 strategies match `reference/trades` trade for trade (same entries, same R); S14
  lacks only the last reference trade, which the research force-closed at the end of the AUDUSD data.
* **Live path:** the real `Runner` driven minute by minute by a fake hub serving USDJPY history (Mar 2024, 7
  strategies) reproduces the replay 30/30 trades with identical R (`tests/test_live_path.py`).

## Deploy (jito FRA host)

Prerequisites, done by the owner:
1. Open the FTMO $100k cTrader demo (hedging) and authorize it on the hub's cTrader grant.
2. Add the alias `ftmo_wf_demo` (with XAUUSD, USDJPY, AUDUSD) to the hub's `data/accounts.production.toml`.
3. Add `wf_s01,…,wf_s20` to the hub's `ALLOWED_ORDER_SOURCES`.
4. The hub must run a build with the read-only `/candles` and `/deals` routes and position labels
   (branch `feat/ctrader-hub-wf-reads`). Deploying it restarts the hub, which also serves callisto's live FTMO trading:
   pick a quiet window.

Then:

```bash
services/walkforward-patterns/deploy/deploy.sh ~/Documents/Markets/VPS/jito.pem ec2-user@<host> --no-start
ssh … 'cd ~/walkforward-patterns/services/walkforward-patterns && cp .env.example .env && chmod 600 .env'  # set WFP_HUB_API_KEY
ssh … '~/walkforward-patterns/.venv/bin/wfp check'     # alias, quotes, candles; bar close should sit nearer the bid
ssh … 'sudo systemctl enable --now walkforward-patterns'
ssh -L 8083:127.0.0.1:8083 …                            # status page http://127.0.0.1:8083
```

Operations: `wfp status`, `wfp disable S07 "reason"` / `wfp enable S07` (a killed strategy stays off until the owner
re-enables it), `wfp flatten --confirm` closes every `wf_*` position (the hub's flatten is 501 on cTrader). Restart is
safe: open positions are reconciled from the hub by label and their time stops resume; a strategy never gets a second
position. Notifications (when enabled): every entry and exit, rejected orders, kill-rule triggers, feed outages over
5 minutes, FTMO breaches (record only), a daily summary at 17:00 New York.
