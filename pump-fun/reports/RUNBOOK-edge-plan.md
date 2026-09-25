# Runbook — edge plan 2026-09-25 (P0 → P4)

Operator steps for the parts of `work-plan-09-25.md` that need wall-clock
time, the VPS, or a human decision. Everything in the code ships **off**:
`mode: dry-run`, `heliusSender.enabled: false`, `execution.dynamicComputeUnits: false`,
`risk.edgeMonitor.enabled: false`, `model.enabled: false`, `entry.mode: immediate`,
`exits.mode: fixed`. Nothing in this runbook is automated: you do each step.

Branch `feat/edge-plan-09-25`, local tag `baseline-2026-09-25` (not pushed).

---

## 1. VPS DB snapshot (P0.3) — do this first

The local `data/scalper.db` is nearly empty, so the real baseline lives on the VPS.

```bash
# on the VPS, in the pump-fun checkout, with the bot STOPPED (or use .backup for a live DB)
mkdir -p data/backup
sqlite3 data/scalper.db ".backup data/backup/scalper.db.20260925-baseline.bak"
sha256sum data/backup/scalper.db.20260925-baseline.bak   # record it in reports/baseline-2026-09-25.md
```

Deploy the branch, then confirm the migrations ran on the first boot: the log shows
no `migrate` errors, and `sqlite3 data/scalper.db "PRAGMA table_info(positions)"` lists
`mint_age_ms`, `fee_tier_bps`, `simulated` and `model_prob`.

## 2. Reproduce the baseline (Gate P1 → P2)

```bash
npm run research:recost -- --csv reports/baseline-2026-09-25/trades-live-7d.csv --seed 1 > /tmp/a.md
npm run research:recost -- --csv reports/baseline-2026-09-25/trades-live-7d.csv --seed 1 > /tmp/b.md
cmp /tmp/a.md /tmp/b.md && grep "real PumpSwap fees" /tmp/a.md
# expect: −0.617 SOL net (−4.55 % of turnover; −5.43 %/trade mean)
```

Gate P1 also needs the live-vs-paper delta below 3 %/trade on the strategy-week live
trades, which comes from the `--live` section of `reports/recost-2026-09-25.md`.

## 3. 48 h dry run on the honest simulator (Gate P2 → P3)

1. Check `config.yaml` has `mode: dry-run`, `guardrails.relaxedRiskEnabled: false`,
   `guardrails.population.enabled: true`, `entry.minAbsoluteSol: 0.04`,
   `fees.feeModel: tiered` and `persistence.priceTickRetentionDays: 30`.
2. Set `experiment.hypothesis` to one sentence (it is saved in `run_sessions`).
3. Start the bot. At boot, confirm:
   - the log has `pumpswap fee tiers loaded`, and **not** `pumpswap fee config fetch failed — using documented schedule`;
   - the dashboard `configHash` changed from the pre-plan session.
4. Leave it running for **48 h**. Don't change the config part-way through, because any
   edit starts a new `configHash` and splits the sample.
5. Export and gate:

```bash
curl -s "http://127.0.0.1:8787/api/reports/trades.csv?range=7d&track=live" -o reports/p2-48h.csv
npm run research:gate -- --phase P2 --csv reports/p2-48h.csv      # exit 0 = met
npm run research:recost -- --csv reports/p2-48h.csv --seed 1 --out reports/p2-48h-recost.md
```

P2 criteria: 0 relaxed trades, 0 non-`pump` trades, an emergency-exit share of net loss
below 25 %, and fees at or under 3.5 % of notional. Profit is not part of this gate.

## 4. Research phase (Gate P3 → P4)

Run these on the VPS DB copy (they are read-only) after the shadow/confirm arms and
`path_ticks` have accumulated:

```bash
npm run research:buckets  -- --csv reports/p2-48h.csv --seed 1
npm run research:exitgrid -- --db data/scalper.db --seed 1 --out reports/exitgrid.md
npm run research:train    -- --db data/scalper.db --min-samples 300 --seed 1
```

- Change **one** thing per experiment (for example `entry.mode: confirm` with the winning
  `delayMs`, `exits.mode: volatility` with the grid's k1/k2, or `model.enabled: true` with
  the trained `model.path`). Record the change in `experiment.hypothesis`. Count every
  variant you try, because that count is `--trials`.
- Then freeze the config and collect **≥ 300 out-of-sample trades**, meaning trades
  made after the parameters were chosen. Gate them:

```bash
npm run research:gate -- --phase P3 --csv reports/oos-trades.csv --capital 0.5 --trials <variants tried>
```

P3 criteria: n ≥ 300, expectancy > 0, 95 % CI lower bound > 0, PF > 1.3,
maxDD < 15 % of capital, and DSR > 0.95.
**Kill criterion:** if no configuration clears P3 on ≥ 300 OOS trades, stop this
strategy and pivot. Don't loosen risk to force volume.

## 5. Live pilot switch-on checklist (P4.2)

Only do this after Gate P3 is **met**. Tick each box yourself.

- [ ] `research:gate --phase P3` output is saved in `reports/` and exits 0.
- [ ] Diff `config.pilot.example.yaml` against the current `config.yaml`, and carry
      over any config.yaml changes made since the pilot file was cut. Keep only the pilot
      overrides: `maxConcurrentPositions: 1`, `dailyLossLimitSol: 0.05`,
      `dailyLossLimitWalletPct: 10`, `consecutiveLossHalt: 5`, `risk.edgeMonitor.enabled: true`
      and `dryRunTwin.enabled: true`.
- [ ] Fund a **dedicated** pilot wallet with exactly **0.5 SOL**. This is the budget, because
      sizing is a % of the wallet. `WALLET_PRIVATE_KEY` points at that wallet.
- [ ] Send path (optional, measured):
  - Set `heliusSender.enabled: true` with `swqosOnly: true`. That gives a min tip of
    0.000005 SOL. Max mode costs at least 0.001 SOL per leg, about 5 % of a 0.04 SOL
    round trip.
  - Set `execution.dynamicComputeUnits: true`.
  - Check the `detect_to_send` p50 on the Edge panel. The target is < 600 ms.
- [ ] Telegram: `/status` answers, `/kill` works, and a `KILL` file halts entries.
      `alerts.chatId` is set, since NEGATIVE_EDGE alerts go there.
- [ ] Dashboard auth is on if the port is reachable beyond localhost.
- [ ] Take a DB snapshot (§1) immediately before switching.
- [ ] **Last step:** set `mode: live` in the pilot file, then start the bot with
      `CONFIG_PATH=config.pilot.example.yaml`.

**While the pilot runs**, watch the Edge panel, which shows the rolling 100-trade
expectancy CI, cohorts, fee % of notional, latency and calibration. NEGATIVE_EDGE
auto-pauses entries when the CI upper bound is < 0 after `minTrades` trades. To resume
you have to decide on purpose: fix or revert the cause, then `touch RESET_DAY`. That
restarts the edge window and the daily accumulators, and is audited in `operator_events`.

**Gate to scale (P4):**

```bash
npm run research:gate -- --phase P4 --db data/scalper.db
```

Requires ≥ 100 live trades, live expectancy > 0, live − twin drag < 3 %/trade, and
failed entries < 15 %. Until it's met, stay at 0.5 SOL and one concurrent position.

## 6. Rollback

- Stop entries now: `/kill` in Telegram, or `touch KILL`.
- Code: `git checkout baseline-2026-09-25` restores the pre-plan behaviour. The new
  DB columns are additive, so the old code ignores them.
- DB: restore `data/backup/scalper.db.20260925-baseline.bak` (with the bot stopped).
