# PumpDesk Work Plan — 2026-09-25

**Scope:** fix every finding from the 7-day trade review (`trades-live-7d.csv`, 524 dry-run trades, 2026-09-22 → 2026-09-25), then build a measurable, defensible edge before any capital goes back live.

**Owner:** Nishimwe Prince · **Status:** proposed · **Supersedes:** nothing (complements `LIVE_PILOT_PLAN.md`, `h4.txt`, `reports/veto-review-2026-09-18.md`)

---

## 0. TL;DR

The bot currently has **no statistically detectable edge** and its dry-run **understates costs by ~2 percentage points per trade** and **overstates fill quality**. Net P&L is −0.348 SOL on 13.57 SOL turnover (−2.57 %/trade); re-costed at real PumpSwap fees it is **−0.62 SOL (−4.57 %/trade)**. Gross (pre-fee) expectancy is −1.15 %/trade with a 95 % bootstrap CI of **[−3.1 %, +0.8 %]** — indistinguishable from random entry with symmetric ±15 % barriers.

The plan runs in five phases, each gated:

| Phase | Goal | Gate to leave phase |
|---|---|---|
| **P0 — Freeze & baseline** (day 0–1) | Stop drift, snapshot data, lock a reproducible baseline | Baseline report committed; live stays off |
| **P1 — Honest simulator** (days 1–4) | Real fees, real latency, pessimistic fills | Re-costed replay of this week matches expectations within ±0.5 %/trade |
| **P2 — Stop the bleeding** (days 2–5) | Remove populations and rules that are provably negative | Relaxed / non-canonical / insta-grad cohorts blocked; emergency-exit loss share < 25 % |
| **P3 — Build the edge** (days 5–21) | Restore signal, add manipulation features, learned filter, adaptive exits | Walk-forward OOS expectancy > 0 with 95 % CI lower bound > 0 over ≥ 300 trades |
| **P4 — Execution & live re-entry** (day 21+) | Land fast, pay less, re-pilot small | Live-vs-twin drag < 3 %/trade over ≥ 100 live trades |

---

## 1. Findings being resolved

Every finding has an ID used throughout the plan.

| ID | Finding | Evidence (this week) | Where in code/config |
|---|---|---|---|
| **F1** | No edge: symmetric ±15 % barriers produce a coin flip | WR 47.3 %, avg win +17.2 %, avg loss −22.0 %, breakeven WR 56.1 %; gross −1.15 %/trade, CI [−3.1, +0.8] | `config.yaml` → `exits.tp1Pct: 15`, `exits.hardStopPct: 15` |
| **F2** | Entry score is flat — carries no information | 421/524 trades scored exactly 85; the 85 bucket is the worst (WR 43.7 %, −4.9 %/trade); scores < 85 (n=103) did **+4.96 % gross** | `src/guardrails/scoring.ts`; baseline 40 + authorities 15 + cleanMint 10 + socials 10 + nameSymbol 5 + rugcheck ≈ 85 for almost every graduation |
| **F3** | Momentum signal disabled for speed | `momentumWindowMs: 0` since 2026-09-16; config comment calls it "the ONE feature that separates winners from craters"; strategy-week live strata: window 0 → PF 0.01, window 250 ms → PF 0.75 | `config.yaml` ~L263–274, `src/enrichment/index.ts:186–199`, `src/enrichment/momentum.ts` |
| **F4** | Paper swap fee is ~5× too low | Modelled 0.25 %/leg; pump.fun docs list **1.25 %/leg** for canonical PumpSwap pools at 0–420 SOL mcap (1.20 % at 420–1,470 SOL). Correction adds −0.271 SOL this week | `config.yaml` L563 `fees.swapFeePct: 0.25`; `src/positions/paperFees.ts:estimatePaperFees` |
| **F5** | Dry-run fills are optimistic | Entry at fresh reserve read (`manager.ts:769`); exit at trigger tick `st.lastPrice` (`dryRunTracker.ts:603`); `exit_trigger_to_confirm_ms` is empty on 524/524 rows. Live week (strategy-week SUMMARY): detect→open avg 1,310 ms, exit confirm avg 1,257 ms, 15/33 entries failed, live WR 22 % | `src/positions/manager.ts` `openPaperLike`, `src/positions/dryRunTracker.ts` |
| **F6** | Stop-loss slippage / gap-through | Trigger −15 %; realized gross median −17.8 %, p10 −27.9 %, worst −47.9 %; + fees → avg −22.3 % | `exits.hardStopPct`, tick cadence `positions.pricePollMs: 500`, LaserStream ticks |
| **F7** | Emergency exits fire too late | 14 trades (2.7 %) = −0.187 SOL = **54 % of net loss**; avg −54.8 %, median −58.9 %; LP-drop trigger at 35 % | `exits.emergencyLpDropPct: 35`, `lpDropWindowTicks: 40`, `src/positions/monitors.ts` |
| **F8** | Take-profit tuning overfit | Replay (pre-reset DB, different population & momentum on) predicted 61 % reach +15 %, WR 71 %, +6.32 %/trade. Realized: 43.7 % hit TP1 | `config.yaml` L291–304 replay table |
| **F9** | Relaxed-risk accepts are negative | relaxed=1: n=156, WR 40.4 %, −6.97 %/trade vs strict WR 50.3 %, −1.97 % | `guardrails.top10HolderCapPct: 45` vs `strictTop10HolderCapPct: 25`; `relaxedRisk*` keys; `tolerate*` flags |
| **F10** | Non-`pump`-suffix mints are a bad population | 26 % of trades, WR 37.5 %, −5.9 %/trade (vs 50.8 %, −2.6 %) — consistent with veto-review segments B/C (1–2 s-old insta-graduations) | `src/detector/index.ts`, `src/guardrails/engine.ts` |
| **F11** | Entering into the sniper spike | Holds < 1 s: WR 32 %; 3–10 s: WR 38 %, −0.32 SOL; > 30 s: WR 69 % | Entry timing — immediately on migration detection |
| **F12** | Fixed per-trade costs dominate small sizes | Fee regression ≈ 0.00036 SOL fixed + 0.74 % of size; at 0.015 SOL (relaxed half-size) fees are **3.2 %** of notional | `entry.minAbsoluteSol: 0.03`, `relaxedRiskSizeMultiplierCap: 0.5`, `fees.estPriorityTipSolPerTx` |
| **F13** | Live send path is plain RPC, no Sender/Jito | `RpcTxSender` over `sendRawTransaction` (skipPreflight, maxRetries 0 ✅) to primary+secondary; `jitoTipSolPerTx: 0` | `src/executor/sender.ts`, `src/executor/broadcaster.ts` |
| **F14** | H4 sellability probe still largely uninformative | Veto review: 128/137 target-population H4 = unknown (`price_moved`), probe measures volatility, not sellability | `src/executor/sellability.ts`, `guardrails.sellabilityProbeSlippagePct: 50` |
| **F15** | Data-completeness gaps block analysis | `venue`/`feed_source` null on 411/524 rows; `entry_tx`/`exit_tx` empty; no score components or earlyFlow in export | `src/persistence/repositories.ts`, `src/dashboard/queries.ts` export |

---

## 2. Phase P0 — Freeze & baseline (day 0–1)

| Task | Detail | Done when |
|---|---|---|
| **P0.1** Keep `mode: dry-run`; confirm `dryRunTwin.enabled: false` stays off for now | No live capital until P3 gate | Config session hash recorded |
| **P0.2** Snapshot DB | `cp data/scalper.db data/backup/scalper.db.20260925-baseline.bak` (tick paths are the replay substrate for P1/P3) | Backup exists; `priceTickRetentionDays` raised to **30** so replay data isn't pruned |
| **P0.3** Commit baseline analysis | Add `reports/baseline-2026-09-25.md` with the table in §1 and the CSV hash | In repo |
| **P0.4** Git tag | `git tag baseline-2026-09-25` so every later change is diffable against it | Tag pushed |
| **P0.5** Fix export completeness (F15) | Always populate `venue`, `feed_source`; add columns: score components JSON, `early_flow_sol`, `early_flow_rate`, `pool_sol_at_entry`, `mint_age_ms`, `entry_detect_to_open_ms`, `creator`, `top10_pct`, `creator_pct`, `mcap_sol_at_entry`, `fee_tier_bps` | New 24 h export has no nulls in those columns |

---

## 3. Phase P1 — Honest simulator (days 1–4)

Nothing gets tuned until the simulator is pessimistic enough that live can only match or beat it.

### P1.1 Real fee model (F4)

- Replace flat `fees.swapFeePct` with the **on-chain PumpSwap fee tiers** (market-cap based `FeeConfig` → `FeeTier { marketCapLamportsThreshold, lpFeeBps, protocolFeeBps, creatorFeeBps }`).
  - Source of truth: pump.fun fee docs and `pump-public-docs` (links in §9). Cache the fee config at startup; refresh every 10 min.
  - Compute mcap per tick from pool reserves × supply; select tier per leg (entry tier and exit tier can differ).
- `src/positions/paperFees.ts`: new `estimatePaperFeesTiered(sizeSol, entryMcapSol, exitFills[])`.
- Keep `swapFeePct` only as an emergency fallback and log a warning when it's used.
- Note `src/executor/pumpAmm.ts` already delegates **live** quoting to the SDK fee model — make paper use the same function so paper and live cannot diverge.
- **Test:** unit test that a 0.03 SOL round trip at 380 SOL mcap costs 2 × 1.25 % + fixed tx costs.
- **Acceptance:** re-running this week's export through the new model yields ≈ −0.62 SOL.

### P1.2 Latency model (F5)

- Build `LatencyModel` from `latency_samples` (already recorded by `recordEntryLatency` / exit latency in `manager.ts:1010–1034, 1316`). Fit empirical distributions for **detect→open**, **exit trigger→confirm**, and **slots-to-land**.
- Paper entry: fill at the pool price **at `t_detect + sampled_entry_latency`**, not at the first read.
- Paper exit: fill at the pool price **at `t_trigger + sampled_exit_latency`** using the recorded tick path (`dryRunTracker.ts:603` must stop using `st.lastPrice` at trigger time).
- If no live samples exist for a percentile, default to the strategy-week averages (1,310 ms entry, 1,257 ms exit) with a p90 of 2.5×.
- Populate `exit_trigger_to_confirm_ms` for every paper exit (simulated value, flagged `simulated=1`).

### P1.3 Fill pessimism & failure model (F5, F6)

- Apply constant-product impact (already in `paperFees.ts`) **plus** a haircut sampled from observed live `fill vs quote` deltas (CHANGELOG: 10–25 % above quote on the first live night).
- Model **entry failure probability** by cohort (live: 15/33 failed). Failed entries are recorded but not traded — this removes survivorship bias from paper results.
- Stop fills: take the **worst price in the latency window**, not the trigger price.

### P1.4 Re-cost & re-baseline

- Run the upgraded simulator over the P0 snapshot and this week's trades.
- Publish `reports/recost-2026-09-2x.md`: logged vs re-costed P&L per exit reason, per cohort.
- **Gate P1 → P2:** re-costed numbers are reproducible (same seed → same result) and live-vs-paper delta on the strategy-week live trades is < 3 %/trade.

---

## 4. Phase P2 — Stop the bleeding (days 2–5, overlaps P1)

Pure removals, low risk, justified by this week's data.

| Task | Finding | Change | Acceptance |
|---|---|---|---|
| **P2.1** Disable relaxed accepts | F9, F12 | `relaxedRiskMaxOpenPositions: 0` (or a new `relaxedRiskEnabled: false`); keep tagging so shadow still records them | 0 relaxed trades in next 48 h; shadow keeps logging them for later re-evaluation |
| **P2.2** Restrict to canonical graduations | F10 | Hard-veto candidates where mint doesn't end in `pump` **OR** mint age < 30 s at migration **OR** pool SOL outside 60–90 SOL (veto-review segment A). Implement as `H12 population` check in `src/guardrails/engine.ts` | Next 48 h: 0 non-segment-A entries |
| **P2.3** Tighten emergency monitors | F7 | `emergencyLpDropPct: 35 → 15`, `lpDropWindowTicks: 40 → 10` (5 s); `creatorDumpThresholdPct: 50 → 20`; **new** `LARGE_SELL` signal: any single sell ≥ X % of pool SOL (start 8 %) seen on LaserStream pool subscription triggers emergency exit before the next poll | Emergency-exit share of net loss < 25 %; median emergency loss better than −35 % |
| **P2.4** Size floor | F12 | Raise `entry.minAbsoluteSol` so fixed costs ≤ 1 % of notional (≈ 0.04 SOL at current tx cost); never trade below it | Fee % of notional ≤ 3.5 % all-in (incl. 2.5 % swap fees) |
| **P2.5** Drop the flat-score gate | F2 | Keep `minEntryScore` but stop using score for sizing until P3.4 ships (score is constant, so the multiplier is noise) | Size determined only by base size + momentum factor |
| **P2.6** Honest H4 | F14 | Leave `price_moved` untolerated; add probe result to export; do not widen tolerance flags. Longer term: replace atomic probe with a **delayed** probe (see P3.2) once the spike settles | H4 status visible per trade |

**Gate P2 → P3:** 48 h dry-run on P1 simulator with P2 changes; report cohort stats. No expectation of profitability yet — the gate is data quality + removal of known-negative cohorts.

---

## 5. Phase P3 — Build the edge (days 5–21)

### P3.1 Restore early-flow momentum (F3)

- Re-enable A/B buckets: `momentumWindowBucketsMs: [0, 250, 500, 1000, 2000]`.
- Run the sample **in parallel** with enrichment (it currently waits after enrichment — `enrichment/index.ts` comment) so the speed cost is ~max(budget, window), not the sum.
- Record `earlyFlow.netInflowSol`, `inflowRateSolPerSec`, **buy count, unique buyers, sell count** within the window.
- Evaluate per bucket on the honest simulator; keep the best window only if it beats window 0 with non-overlapping CIs.

### P3.2 Delayed / confirmation entry (F11)

Hypothesis: the first seconds after migration are sniper-dominated; the edge is in **second-wave** entries.

- New entry mode `entry.mode: confirm` with `confirmDelayMs` ∈ {5,000, 15,000, 30,000}.
- Enter only if during the delay: net inflow > threshold, price above migration price by 0–25 % (not already blown out), no single sell > 8 % of pool, ≥ N unique buyers.
- Delayed H4 sellability probe (the pool is calmer → probe actually reaches the sell leg).
- Shadow-evaluate all three delays simultaneously on every canonical graduation (shadow infra already exists in `src/guardrails/shadow.ts`; raise `shadow.maxConcurrent` accordingly).

### P3.3 Manipulation & population features

Grounded in the 15.2 M-coin pump.fun study (§9, [R4]): top 1 % creator clusters control 58.6 % of coins; wash trading is 17 % of all transactions and inflates graduation; copycats graduate at 0.86 % vs 9.2 % for originals; coordinated atomic dumps are common.

| Feature | Source | Use |
|---|---|---|
| **Creator cluster size** — wallets sharing funding source (1–2 hops) | Helius enhanced tx / funding graph; cache in SQLite | Veto clusters with > K launches in 7 d (extends `creatorMaxLaunches7d` to clusters) |
| **Bundle share at launch** — % supply bought in creation slot / same-slot bundles | Bonding-curve tx history (Jito bundle detection) | Score feature + veto above threshold |
| **Wash-trade ratio** — same-wallet or same-tx buy+sell cycles on the curve | Curve tx history | Score feature |
| **Copycat flag** — name/symbol/image hash collides with an earlier coin | Metadata + perceptual hash of image | Veto or heavy penalty |
| **Sniper concentration post-migration** — % pool buys from known sniper wallets | Maintained sniper list from own logs | Score feature (high = expect dump) |
| **Holder quality** — fresh-wallet ratio among top 20; ex-vault/ex-curve top10 (H5 fix from `h4.txt`) | Holders enrichment | Score feature |
| **Time-to-graduate** — creation → migration duration | Token age API / on-chain | Score feature (very fast = bundled) |
| **Social** — has X/TG, account age | Metadata | Weak feature |

### P3.4 Replace hand score with a learned filter (F2, F8)

- **Labels:** triple-barrier labels on **every shadow-tracked canonical graduation** (not only trades) using the honest simulator: upper = TP, lower = SL, vertical = time stop; barriers **volatility-scaled** [R6].
- **Primary model:** the rule set (P2 population + P3.2 confirmation).
- **Meta-model:** gradient-boosted trees / logistic regression predicting P(primary signal is profitable after costs) — meta-labeling decides **take/skip and size** [R6].
- **Validation:** purged, embargoed k-fold / walk-forward by day; report **Deflated Sharpe Ratio** and **Probability of Backtest Overfitting** because we're testing many variants [R7][R8][R9].
- Keep it simple and inspectable: log feature importances and per-trade SHAP-style contributions to the dashboard.
- Module: `src/guardrails/model.ts` (inference, JSON-exported weights) + `scripts/train/` (Python or TS offline training). Model version stored per trade.

### P3.5 Adaptive exits (F1, F6, F8)

- Replace fixed ±15 % with barriers scaled to realized volatility over the confirmation window (e.g. TP = k₁·σ, SL = k₂·σ, clipped).
- Grid-search k₁/k₂/time-stop **only via walk-forward** on tick paths; never pick from a single in-sample replay again (lesson from F8).
- Re-evaluate the dead-money exit (`deadMoneyEnabled`) and time stop inside the same walk-forward.
- Stop placement must account for P1.2 latency: a stop at −15 % with ~1.3 s exit latency realizes ~−18 %… so model and choose on **realized**, not trigger levels.

### P3.6 Experiment discipline

- One change per config session; `config-sessions.json` hash already tracks it — add a `hypothesis` field.
- Minimum sample per arm: 300 trades (or 300 shadow labels) before a decision.
- Decision rule: adopt only if OOS expectancy CI lower bound > 0 **after real fees**.

**Gate P3 → P4:** Walk-forward OOS over ≥ 300 trades on the honest simulator: expectancy > 0, 95 % CI lower bound > 0, profit factor > 1.3, max drawdown < 15 % of allocated capital, DSR > 0.95.

---

## 6. Phase P4 — Execution & live re-entry (day 21+)

### P4.1 Faster, more reliable landing (F5, F13)

- Add a **Helius Sender** `TxSender` (dual-routes staked SWQoS + Jito in parallel) alongside existing RPC senders in `src/executor/broadcaster.ts` [R3][R10].
- Dynamic Jito tip from the tip-floor API (use ~p75 + buffer; Sender minimum 0.0002 SOL on Jito path) [R3][R11].
- Keep `skipPreflight: true`, `maxRetries: 0` (already correct in `sender.ts`); keep presigned exit ladder (`positions/presign.ts`).
- Compute-unit tuning: simulate once, set CU limit tightly, priority fee from recent fee percentiles.
- Co-locate the bot process near Helius region used by LaserStream; measure detect→send p50/p95.

### P4.2 Twin-first live pilot

- Turn on `dryRunTwin.enabled: true` with the honest simulator so every live trade has a paper twin; `netPnlDelta` = execution drag.
- Pilot budget: 0.5 SOL; `maxConcurrentPositions: 1`; daily loss limit 0.05 SOL; halt after 5 consecutive losses (existing breaker).
- **Gate to scale:** ≥ 100 live trades, live expectancy > 0, live-vs-twin drag < 3 %/trade, failed entries < 15 %.

### P4.3 Monitoring

- Dashboard panels: rolling 100-trade expectancy with CI, cohort breakdown, fee % of notional, latency p50/p95, emergency-exit share, model calibration curve.
- Telegram alert when rolling expectancy CI upper bound < 0 → auto-pause.

---

## 7. Work breakdown & timeline

| Day | Work |
|---|---|
| 0–1 | P0.1–P0.5 |
| 1–2 | P1.1 fee tiers + tests; P2.1, P2.2, P2.4, P2.5 config/code |
| 2–4 | P1.2 latency model, P1.3 fill pessimism, P2.3 emergency monitors + LARGE_SELL |
| 4 | P1.4 re-cost report → **Gate P1** |
| 5–7 | 48 h dry-run → **Gate P2**; start P3.1 buckets + P3.2 shadow delays |
| 7–12 | P3.3 feature pipeline (cluster, bundle, wash, copycat) |
| 12–17 | P3.4 labels + meta-model + purged walk-forward |
| 17–21 | P3.5 adaptive exits; full OOS evaluation → **Gate P3** |
| 21+ | P4.1 Sender/Jito, P4.2 pilot, P4.3 monitoring |

---

## 8. KPIs & kill criteria

| KPI | Now | P2 target | P3 gate | Live gate |
|---|---|---|---|---|
| Expectancy after real fees | −4.57 %/trade | measured, not optimized | > 0, CI LB > 0 | > 0 over 100 trades |
| Win rate vs breakeven WR | 47 % vs 56 % | — | WR ≥ breakeven + 5 pts | same |
| Emergency-exit share of loss | 54 % | < 25 % | < 20 % | < 20 % |
| Median stop realization | −17.8 % gross | reported with latency | within 3 pts of modeled | within 3 pts of twin |
| Fees % notional | 2.1 % (under-modeled) | ≤ 3.5 % true | ≤ 3.5 % | ≤ 3.5 % |
| Detect→open latency | 1,310 ms avg (live) | — | — | p50 < 600 ms |
| Failed entries | 45 % (live) | modeled | modeled | < 15 % |

**Kill criteria:** if after P3 no configuration clears the gate on ≥ 300 OOS trades, stop the post-graduation scalping strategy and pivot (e.g., pre-graduation lane `pregrad`, or longer-horizon second-wave holds) rather than loosening risk to force volume.

---

## 9. References

**Protocol & fees**
- [R1] Pump.fun — Fees (bonding curve 1.25 %; PumpSwap canonical tiers 1.25 % → 0.30 % by mcap): https://pump.fun/docs/fees
- [R2] pump-fun/pump-public-docs (PumpSwap program, creator fee, fee config): https://github.com/pump-fun/pump-public-docs · https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md · https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_CREATOR_FEE_README.md
- DeFiLlama — Pump fees/volume: https://defillama.com/protocol/pump

**Execution**
- [R3] Helius — Achieving zero-slot execution with Sender and LaserStream: https://www.helius.dev/blog/zero-slot
- [R10] Helius Sender docs: https://www.helius.dev/docs/sending-transactions/sender
- Helius staked connections: https://www.helius.dev/staked-connections
- [R11] Jito — Low-latency transaction send (bundles, tip accounts, tip floor): https://docs.jito.wtf/lowlatencytxnsend/
- Jito JSON-RPC reference: https://github.com/jito-labs/mev-protos/blob/master/json_rpc/http.md

**Market microstructure & manipulation**
- [R4] Meme Coin Factories: Uncovering Large-Scale Manipulations on pump.fun (arXiv): https://arxiv.org/html/2609.10246
- [R5] Predicting the success of new crypto-tokens: the Pump.fun case (arXiv 2602.14860): https://arxiv.org/abs/2602.14860
- The Memecoin Phenomenon: An In-Depth Study of Solana's Blockchain Trends (arXiv): https://arxiv.org/html/2512.11850v3
- Resisting Manipulative Bots in Meme Coin Copy Trading (arXiv): https://arxiv.org/html/2601.08641v2

**Methodology**
- [R6] Triple-barrier labeling & meta-labeling (mlfinpy): https://mlfinpy.readthedocs.io/en/latest/Labelling.html
- [R7] Bailey & López de Prado — The Deflated Sharpe Ratio (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- [R8] Purged cross-validation: https://en.wikipedia.org/wiki/Purged_cross-validation
- [R9] Backtest overfitting in the ML era — comparison of out-of-sample methods (Knowledge-Based Systems): https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110

**Internal**
- `h4.txt` — H4/H5/H6 guardrail diagnosis (2026-09-17)
- `reports/veto-review-2026-09-18.md` — candidate populations A–E
- `reports/strategy-week/SUMMARY.md` — live week (18 trades, WR 22 %, latency)
- `CHANGELOG.md` — momentum disable, fee constant change, live entry fixes
- `trades-live-7d.csv` — 524-trade dry-run export analysed here

---

## Appendix A — Key numbers from the 2026-09-25 review

| Exit reason | n | Net SOL | Avg net % | Gross median % | MFE avg % | Median hold |
|---|---|---|---|---|---|---|
| TAKE_PROFIT_1 | 229 | +1.139 | +18.5 | +17.9 | 20.8 | 6.8 s |
| STOP_LOSS | 233 | −1.281 | −22.3 | −17.8 | 3.4 | 4.6 s |
| TRAILING_STOP | 48 | −0.019 | −1.8 | +1.0 | 13.4 | 9.0 s |
| EMERGENCY_EXIT | 14 | −0.187 | −54.8 | −58.9 | 4.8 | 4.4 s |
| **Total** | **524** | **−0.348** | **−2.57** | — | — | 5.7 s |

| Cohort | n | Gross % | Net % (logged) | Net % (real fees) | WR |
|---|---|---|---|---|---|
| All | 524 | −0.44 | −2.57 | −4.57 | 47 % |
| Strict only | 368 | +0.20 | −1.71 | −3.71 | 50 % |
| `pump` suffix | 388 | +0.31 | −1.80 | −3.80 | 51 % |
| Strict + `pump` | 281 | +0.84 | −1.07 | −3.07 | 53 % |
| Score < 85 | 103 | +4.96 | +2.83 | +0.83 | 62 % |

*Score < 85 is a small, unexplained cohort — investigate its score components in P3.4 before acting on it.*

| Hold time | n | Net SOL | WR |
|---|---|---|---|
| < 1 s | 59 | −0.097 | 32 % |
| 1–3 s | 117 | −0.037 | 49 % |
| 3–10 s | 173 | −0.322 | 38 % |
| 10–30 s | 110 | +0.035 | 55 % |
| > 30 s | 65 | +0.072 | 69 % |
