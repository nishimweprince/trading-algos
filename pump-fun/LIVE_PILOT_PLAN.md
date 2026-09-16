# Live Pilot Plan — 1 SOL wallet

Written 2026-09-16 from the 7-day dry-run twin export (`trades-dry-7d.csv`,
1,838 paper trades, 2026-09-10 → 09-14). Baseline: **+7.73 SOL net on ~358 SOL
turnover** (+2.2%), 79% win rate, with all −21 SOL of losses concentrated in
STOP_LOSS and 55 rugs (< −80%) alone costing −10 SOL.

`config.yaml` is already switched to `mode: live` with the parameters in §1.

**Status 2026-09-16:** Phases 1, 2 (code), 4 (infrastructure) and the §7 code
work are implemented and tested (`npm test`: 376 passing). What remains is
operational: Phase 0 pre-flight, the Phase 3 live gates (5 days / 150
trades), running the rug forensics against the server DB, and flipping the
Business block when the plan lands. Each item below is marked ✅ (done in
code), 🟡 (runs itself once live, needs review) or ⬜ (operator action).

---

## 1. Live parameters now in `config.yaml`

| Knob | Value | Why |
| --- | --- | --- |
| `mode` | `live` | — |
| `entry.min/base/maxSizeWalletPct` | 5 / 10 / 12 (0.05 / 0.10 / 0.12 SOL) | Round-trip fee ≈ 0.003 SOL; ≥ 0.08 SOL keeps drag < 3%. Min stays at 5% so `momentumSizeFloorMultiplier: 0.4` still bites. |
| `entry.minAbsoluteSol` | 0.03 (was 0.01) | Below this every trade is fee-negative. |
| `risk.maxConcurrentPositions` | 2 (was 1) | Mean overlap in the dry run was ~1.7; one slot drops ~half the flow. 2 × 0.12 = 24% deployed max. |
| `risk.dailyLossLimitSol` / `…WalletPct` | 0.15 / 15% | One full-size rug (−0.10) + ordinary stops before halt, instead of halting on the first loss. |
| `risk.emergencyExitCount24h` | 8 (was 5) | LP-pull / creator-dump monitors will actually fire live; 5 deadlocked a prior session. |
| `guardrails.relaxedRiskMaxSizeWalletPct` | 5 (was 3) | 3% of 1 SOL is below fee-efficient size. |
| `positions.pricePollMs` | 500 (was 1000) | ~2 RPS on a 50 RPS plan; halves tick-latency give-back on trailing (~4 pts) and stop overshoot (~5 pts). |
| `exits.lpDropWindowTicks` | 40 (was 20) | Keeps the LP-drop window at 20 s with the faster poll. |
| `fees.estPriorityTipSolPerTx` | 0.0002 (was 0.001) | Jito is off; real cost is ~0.00006–0.00125. The old value charged ~4 SOL of phantom fees in 7 days. |
| `LIVE_RISK_WALLET_PCT` in `.env` | no longer read | Rungs are explicit above. |

Unchanged on purpose: `hardStopPct 15`, `timeStopMinutes 10`, TP ladder,
guardrail caps, Jito off, LaserStream off (Developer plan), twin **on** with
`sizeMode: mirror` — the twin-vs-live Δ is the pilot's primary instrument.

**Wallet arithmetic at 1 SOL:** `WALLET_FLOOR` = 0.1 + 0.03 = 0.13 SOL. Two
max-size opens need 0.13 + 0.24 = 0.37 SOL free → fine. The daily-loss breaker
trips at −0.15 SOL realized.

---

## 2. Phase 0 — Pre-flight (before the first live trade)

1. ⬜ Fund the wallet with 1 SOL; confirm `WALLET_PRIVATE_KEY` in `.env` matches.
2. ⬜ Add a real `SECONDARY_HTTP_URL` (Alchemy/QuickNode free tier). The config
   comment is explicit: a single exhausted RPC becomes a silent 100% veto in
   live mode. Currently blank in `.env`.
3. ✅ `npm run typecheck && npm test` — 39 files / 376 tests green on this tree.
4. ⬜ Start, confirm in Telegram: feeds up (PumpPortal + Helius WS), wallet
   balance read, breakers all green, `mode=live`. Expect the log line
   `dry-run twin is running EXIT OVERRIDES` — that is the S1 lane, by design.
5. ⬜ Kill switch path rehearsed: `/kill` from the admin Telegram user, and the
   H10 KILL-file sentinel.

**Gate:** first 3 live entries reach `OPEN` with a reconciled token balance and
a pre-signed exit ladder; no `EXITING` position stuck > 30 s.

---

## 3. Phase 1 — Make the twin honest (T1–T4) — *code, ~1 day*

The dry-run numbers cannot be trusted as a live forecast until the twin runs
the same defences as live and charges the costs live pays.

### T1. Emergency monitors in the twin ✅
[src/positions/dryRunTracker.ts](src/positions/dryRunTracker.ts) only calls
`PaperPosition.onPrice`; the live manager also runs `EmergencyMonitor`
([manager.ts:790](src/positions/manager.ts:790), [monitors.ts](src/positions/monitors.ts)).
Zero `EMERGENCY_EXIT` rows in 1,838 twin trades confirms the gap.

- In `open()`, build an `EmergencyMonitor` from `monitorCfgFor(relaxedRisk)`
  (lift that helper out of `PositionManager` into `monitors.ts` so both share it).
- Register the creator ATA with the poller (`registerPricing` logic in
  `manager.ts:769` — extract to a shared helper) so `creatorBaseBalance` arrives
  on twin ticks.
- In `onTick`, run the monitor before the FSM; on a signal call
  `finish(mint, 'EMERGENCY_EXIT')` (extend `forcedTrigger` union).
- Test in `test/dry-run-twin.test.ts`: inject ticks with a 40% quote-reserve
  drop and a creator balance halving; assert `EMERGENCY_EXIT`.

### T2. Exit slippage in paper fills ✅
Twin fills at the tick mid-price; live sells `rawBase` into a constant-product
pool. Add `slippageForSell(baseIn, baseReserve, quoteReserve, feePct)` (there is
already curve math in `src/executor/slippage.ts` / `pumpAmm.ts` — reuse it) and
apply it in the twin's `netPnl`/fill path using the tick's reserves. This turns
the −99% rug fills into realistic −100% and, more importantly, makes ordinary
exits pay impact.

### T3. Attribution columns ✅
`dry_run_positions` rows leave `feed_source`, `venue`, `entry_soft_score`,
`exit_trigger_to_confirm_ms` null (see `persist()` in `dryRunTracker.ts` vs
[repositories.ts:290](src/persistence/repositories.ts:290)). Thread the accept
event's `feedSource`, `venue`, and score into the twin persist; for the twin,
`exit_trigger_to_confirm_ms` = tick-to-FSM latency (0 today, non-zero once
LaserStream pushes ticks). Without these the Business A/B in §7 is unmeasurable.

### T4. Fee model ✅
Done in config (`estPriorityTipSolPerTx: 0.0002`). Add a `fees.jitoTipSolPerTx`
knob defaulting to 0 so re-enabling Jito later is one line, not a re-tune.

Implementation notes: `monitors.ts` exports `monitorCfgFor` / `creatorAtaFor`
shared by both legs; `paperFees.ts` has the impact math; twin rows carry
`slippage_sol`, `feed_source`, `venue`, `entry_soft_score`,
`exit_trigger_to_confirm_ms`, `exit_overrides_json`; the dry CSV emits them.
Tests: `test/dry-run-twin-fidelity.test.ts`, `test/paperFees.test.ts`.

**Gate 🟡:** twin re-run on the live feed for 48 h shows `EMERGENCY_EXIT` rows,
non-zero slippage, filled attribution columns, and live-vs-twin `netPnlDelta`
median within ±1.5 pts per trade.

---

## 4. Phase 2 — Strategy fixes the data already supports (S1–S3) — *~1 day*

### S1. Dead-money exit ✅ (twin lane) / ⬜ (promote to live)
51% of trades sat the full 10 min for a +3.7% median; the 55 rugs sat flat
(MFE ≈ +4%) for a median 234 s before dying. Both are the same pattern: no
follow-through after graduation.

- New knobs in `schema.ts` + `config.yaml`:
  `exits.deadMoneyMinutes: 3`, `exits.deadMoneyMaxMfePct: 5`.
- In [engine.ts](src/exits/engine.ts) `evaluateExit`: if `!tp0Done && !tp1Done`
  and `nowMs − openedAtMs ≥ deadMoneyMinutes` and
  `gainPct(entry, highWaterPrice) < deadMoneyMaxMfePct` → return
  `{ trigger: 'TIME_STOP', sellFraction: 1, reason: 'dead money' }` (reuse the
  trigger; put the distinction in `reason` so dashboards keep working).
- Start with the twin only: `dryRunTwin.exitOverrides` runs the variant while
  live runs the baseline — this is what `config.yaml` ships now. Compare after
  48 h on the Δ dashboard (filter twin rows by `exit_overrides_json`), then
  promote by copying the keys into `exits:` and deleting the override block.

### S2. Volatility/momentum sizing check ✅
`high_volatility=1` trades were 14% of volume and 60% of profit (+4.67 vs
+3.05 SOL). Verify `sizeMultiplier × momentumFactor` in
[pipeline.ts:103](src/guardrails/pipeline.ts:103) actually spreads sizes across
5–12% (the export shows every trade at ≈0.19 SOL, i.e. sizing was flat). If it
is flat because `momentumSizeFullInflowSol` is too high for the observed
inflows, recalibrate from the DB `features` JSON (`momentumNetInflowSol`).

Finding: it was flat because the dry run ran `min = base = max = 5%`, so the
floor clamp swallowed every momentum reduction. The 5 / 10 / 12 % ladder in §1
fixes that (`test/sizing.test.ts` pins it). ⬜ Re-check `size_sol` spread after
the first live day; recalibrate `momentumSizeFullInflowSol` only if sizes still
cluster at one rung.

### S3. Rug forensics ✅ (tool) / ⬜ (run on server data)
Pull the 55 rug mints from the CSV, join to `dry_run_positions.pricingJson` /
`features` in `data/scalper.db`, and tabulate: creator holdings %, top-10 %,
pool SOL at entry, creator launch count, whether the top holders were funded
by one wallet (bundle). Whatever separates them from the 1,783 non-rugs
becomes either a hard cap tighten or a size-down. Persist creator blacklists
across restarts if they are not already (`H8`).

Tool: `npm run report:rugs -- --track dry --range 7d` on the server, or
`GET /api/reports/rug-forensics?track=dry&range=7d&format=md` on pumpdesk.
It joins trades to `candidates` features (creator / top-10 / max-holder share,
pool SOL, buy impact, early inflow, rugcheck, soft score, enrichment ms) and
lists repeat-rug creators. The local `data/scalper.db` holds only 6 twin rows —
the 1,838-trade week lives on the server. ⬜ Run it there; a feature whose rug
p50 sits above the clean p75 is the cap to tighten.

**Gate:** twin variant with S1 beats baseline on net SOL over ≥ 300 trades;
rug rate among accepts < 2%.

---

## 5. Phase 3 — Live pilot gates 🟡

Run live with the §1 parameters for **5 trading days** or **150 live trades**,
whichever first. Review daily from the Live / Dry-run / Δ dashboard.

| Metric | Pass | Action if fail |
| --- | --- | --- |
| Live − twin `netPnlDelta` (median, pts) | ≥ −2.0 | Execution drag is eating the edge → stop, fix sends (Business staked connections or Jito) before continuing |
| Entry→open latency (`detectToOpenMs`) p50 | < 3.5 s | Cut `enrichmentBudgetMs`; move to Business |
| Exit trigger→confirm p50 | < 2.5 s | Same |
| Stuck `EXITING` positions | 0 | Kill switch, investigate ladder/blockhash |
| Daily-loss halts in 5 days | ≤ 1 | Tighten `dailyLossLimitWalletPct` to 10, size base to 8% |
| Net after fees | > 0 | Do not scale; return to §4 |

Only after all six pass: `maxConcurrentPositions: 3`, and consider funding to
2 SOL (sizes stay at the same percentages).

---

## 6. Phase 4 — Target ladder experiment ✅ (lane) / ⬜ (run)

Reach rates: +22% → 18.8%, +30% → 10.9%, +40% → 6.9%, +50% → 1.4%. TP0 at 22
is doing the work; TP1 at 40 is nearly decorative. The `exitOverrides` lane
accepts `tp0*`, `tp1*`, `tp2Pct`, trailing and stop knobs, so after the S1
variant has its 300 trades, swap the override block to
`tp1Pct: 30, tp1SellFraction: 0.5` and run the next comparison. One variant at
a time — the row stamp is the whole override set, not a diff.

---

## 7. Helius Business upgrade — when it lands

Follow the commented block at the bottom of `config.yaml`. In order of payoff:

1. ✅ **LaserStream account-subscribe on pool vaults + creator ATA**
   (`src/positions/laserstreamPricing.ts`) as a push tick source into the same
   `onTick` path for the live manager, the twin and shadow. Poller stays as
   the liveness fallback. ⬜ Flip `positions.laserstreamTicksEnabled: true`
   with `HELIUS_GRPC_URL` / `HELIUS_GRPC_TOKEN` set.
2. ⬜ `detector.laserstreamEnabled: true` — graduation from the migrate
   instruction (already implemented); drops the detect→enrich→enter gap that
   produced 32 instant −18…−64% stops.
3. ⬜ Staked-connection sends (`sendTransaction` 50/s) — no code change; the
   Helius endpoint routes them. This is the precondition the config states
   for a 15% hard stop.
4. ⬜ A/B the pre- vs post-upgrade weeks: `feed_source` and
   `exit_trigger_to_confirm_ms` are now on every twin row for exactly this.

What it will **not** fix: 39 trades at −90%+ that were single-tx LP pulls.
That is §4 S3 and sizing, not latency.

---

## 8. Order of work

```
Phase 0 pre-flight ──► go live (§1 params) ──► Phase 1 T1–T4 (twin honesty)
                                            └► Phase 2 S1–S3 in the twin variant
                                            └► Phase 3 gates (5 days / 150 trades)
                                                     └► scale (3 slots / 2 SOL)
                                                     └► Business upgrade (§7)
```

Phases 1 and 2 are pure code + twin; they run while live trades in the
background and do not require a restart of the live leg until promoted.
