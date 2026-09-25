**Baseline tag:** `baseline-2026-09-25` (local) · **Mode:** `dry-run`, `dryRunTwin.enabled: false` (frozen, P0.1) · **Tick retention:** 30 days (P0.2)

Every later change in `work-plan-09-25.md` is measured against this report. The tables below are regenerated from the committed CSV. They are not copied by hand, so anyone can reproduce them:

```bash
npm run research:recost -- --csv reports/baseline-2026-09-25/trades-live-7d.csv --seed 1 \
  --title "Baseline — 2026-09-25" --preamble reports/baseline-2026-09-25/preamble.md \
  --out reports/baseline-2026-09-25.md
```

Note: "%/trade" figures here are **per-trade means**. The work plan's −2.57 % / −4.57 % figures are **turnover-weighted** (net SOL ÷ turnover), and both appear in the totals table.

## Findings index (from work plan §1)

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
