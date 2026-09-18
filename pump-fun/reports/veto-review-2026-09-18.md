# Veto review — 2026-09-18 (post 57d9196 deploy)

Window: 2026-09-17 20:41 → 2026-09-18 08:04 UTC (11.6 h, run session 21, commit 57d9196,
config: top10 45 / creator 8 / minPoolSol 25 / sellability ALT on).

**542 candidates → 542 vetoes → 0 accepts → 0 positions.**

Yesterday's fixes did what they were meant to (ALT: 495/542 probes assembled at
792–1109 bytes, `usedLookupTable=1`; H5 curve-holding exclusion live). They moved
the wall, they did not remove it. The post-deploy wall is one check on one
population, plus two populations that should be vetoed but are being vetoed
under the wrong label.

## 1. The candidate flow is three different populations

Segmented by pool SOL at enrichment and whether the mint account was readable:

| Segment | n | What it is | Evidence |
|---|---|---|---|
| **A. 60–80 SOL pool, mint readable** | **137** | Genuine pump.fun curve graduations (~67 SOL migration). **This is the target population.** | H1/H2/H3/H7/H8/H9/H10 pass 137/137; soft score median 85, none <60 |
| B. 60–80 SOL pool, mint *unreadable* | 67 | Bundled insta-graduations: mint created **1–2 s** before MigrateV2 | 12/12 sampled mints created in the migration slot; 8/8 sampled pools at x0.01 hours later (bundle-and-dump) |
| C. 80–5,000 SOL pool, mint unreadable | 179 | 1–2 s-old tokens landing with 200–5,000 SOL — not a classic curve graduation | Bimodal: 1000+ SOL pools sit at x1.05–1.3 (un-scalpable); 200–500 SOL ones mostly drained to <0.5 SOL |
| D. <60 SOL pool | 118 | Never-funded / junk pools (median 0.1 SOL) | 0/20 sampled hold ≥20 SOL now; pre-reset shadow: sole-H7 8% win, −0.011 SOL/trade |
| E. no pool | 45 | Spurious migration events (18 from PumpPortal) | — |

"Mint account unavailable" (256 = 47%) is **not an RPC bug** — every sampled case
is a token created 1–2 s before it migrated, so nothing (RPC at processed, DAS,
rugcheck, token-age API) has indexed it yet. It is a population marker.

## 2. Why population A (the one we want) produces zero trades

| Check | pass | fail | unknown |
|---|---|---|---|
| H4 sellability | **1** | 8 | **128** |
| H5 concentration (cap 45) | 89 | 41 | 7 |
| H6 creator (cap 8) | 116 | 14 | 7 |
| everything else | 137 | 0 | 0 |

**96 of 137 (~200/day) are clean on every check except H4.** H4 is now doing on
population A exactly what H5's 25% cap did yesterday: veto everything it can measure.

### H4 unknown = `price_moved` (128) — the probe is measuring volatility, not sellability

`price_moved` is PumpSwap `ExceededSlippage` (6004) at instruction index 6/7 —
verified to be the **buy** ix in both layouts (7 when the fresh pool still needs
the SDK's `extendAccount`). The probe buys 0.02 SOL with `PROBE_SLIPPAGE_PCT = 15`
(hardcoded, `src/executor/sellability.ts:68`); a graduation being sniped moves
>15% between the SDK state read and the simulation on ~95% of real graduations.
The sell leg never runs, so the check learns nothing about sellability.

The engine never tolerates `price_moved` (engine.ts:169) because of the 09-16
trades. Those 9 trades were all `relaxed_unknown_h4` via the *buy-only backstop*
(pre-ALT), not `price_moved`: 4 stop-losses at −17…−26% in 3–15 s, 5 small trail
wins, net −0.02 SOL. The lesson from them is "don't enter blind behind a spike",
not "a 15% probe bound is the right volatility gate" — and the entry path already
has its own bounds (`maxSlippagePct 5`, `buyRetrySlippageTiers [8]`).

### H4 fail (8 in A, 4 vetoed on H4 alone) — two false-honeypot mechanisms

- `[2, Custom 2004]` (e.g. 4zv9pg, 88mVzq, 9tPg7E, 47mdJM): index 2 is the SDK's
  `extendAccount` helper hitting an Anchor constraint error. `classifySellabilityError`
  defaults anything unrecognised to `sell_failed` → hard fail.
- `[9|11, Custom 1]` (e.g. 8P4hKQ, H9TrEv): token `InsufficientFunds` on the sell
  ix. `sellAmount = 90%` of a fee-less constant-product estimate from
  *enrichment-time* reserves; any ≤15% up-move before the probe means the buy
  returns fewer tokens than the sell tries to move.

Neither says anything about the token being sellable.

## 3. Recommendations

### Fix, don't relax: H4 (code, `src/executor/sellability.ts`) — unblocks ~200/day

1. **Widen the probe bound** (`PROBE_SLIPPAGE_PCT` 15 → 50+, or re-quote once on
   6004). A honeypot fails the sell leg at any bound; the bound only decides
   whether the probe gets *to* the sell leg. Make it a config key.
2. **Derive `sellAmount` from the SDK's own buy quote** (`buyQuoteInput` returns
   `base`) × ~0.8, not from enrichment-time reserves.
3. **Classify by instruction index**: error before the buy ix → `account_setup_unavailable`
   (unknown, tolerable); at the buy ix → `price_moved`/`buy_failed`; at/after the
   sell ix → `sell_failed` (the only true fail).
4. **Surface the move explicitly**: record `poolMovePct` (probe-time vs
   detection-time quote reserve) on the candidate and give it its own knob
   (hard cap and/or soft-score term). Today a 15% early move is a *hidden hard
   veto* inside H4 while the strategy's momentum signal is switched off
   (`momentumWindowMs: 0`) — inverted for a momentum entry. The operator should
   set that threshold on purpose, with shadow data (below).

Do **not** simply add `price_moved` to the tolerate list: the 09-16 stop pattern
is real, and entries would mostly bounce off the 5/8% entry bounds anyway.

### Abandon explicitly (they are already being vetoed, under the wrong labels)

- **Populations B and C (unindexed 1–2 s-old mints, 47% of flow).** Today they
  die as `LOW_SCORE` (score pinned at baseline 40 because there is no mint/metadata
  to score — 128 of 143 LOW_SCORE vetoes) or as `UNKNOWN:H1/H2/H9` when something
  else also fails. Make it a named veto (e.g. `UNINDEXED_MINT`: mintInfo *and*
  metadata *and* tokenAge all missing while the pool is present) and keep it a
  hard veto. Important sequencing: if anyone adds a retry to the mint read
  (which would succeed — the account exists), these 179+67 candidates would start
  scoring 65 and flow to entry with no outcome data. Add the named veto first.
- **Population D via H7 (minPoolSol 25).** Keep as is. Every fail is a <1 SOL pool
  and the sole-H7 shadow cohort lost money. Do not take the WS10 "25 → 15" step;
  there is nothing between 1 and 60 SOL worth having.
- **LOW_SCORE as it stands.** On population A no candidate scores <60 (p10 = 65),
  so the gate costs nothing there; its 143 vetoes are all the data-gap case above.
  Once `UNINDEXED_MINT` exists, LOW_SCORE goes back to meaning what it says.

### Leave alone for now

- **H5 at 45.** Not the bottleneck on A (27 sole-H5 blocks; 18 of them are 60–80%
  top-10, i.e. bundled supply on a 67 SOL pool, 6 are in 45–60). Top-10 on real
  graduations is bimodal (median 27%, p75 60%), and 45 sits in the gap. Pre-reset
  shadow (n=166 sole-H5, measured with the old inflated snapshot) showed no
  monotonic harm from concentration, so 45 → 60 is defensible later — decide on
  fresh shadow data, not now.
- **H6 at 8.** Zero sole blocks on A.
- **H1/H2/H3/H8/H9/H10.** 100% pass on A; nothing to relax.
- **`tolerateUnknownWhenNoHardFail`.** Working as intended; keep.

### Turn the instrument back on

`shadow.enabled: false` since 9673d28 (2026-09-10, no reason recorded). It is the
only capital-free way to answer the two open calibration questions here (what
early-move threshold, and whether 45 → 60 on H5 is safe). It produced 2,938 exit-FSM
outcomes in four days last time. Re-enable before or with the H4 change so the
first day of accepts has a counterfactual baseline next to it.

## 4. Expected effect

With H4 fixed and nothing else changed: ~96/11.6 h ≈ **200 accepts/day** from
population A (vs 0), all with H1–H3/H7–H10 clean and score ≥65, sized by the
existing relaxed-risk rules. Whether they are *good* entries is the volatility
question the explicit `poolMovePct` knob + shadow data are there to settle; the
guardrail stack itself is no longer the reason nothing trades.

Scripts used (scratchpad, not committed): veto.mjs (per-check matrix), enr.mjs
(unknown combos / timing), age.mjs (mint creation slot vs migration slot),
now.mjs (vault balances now vs at graduation), pop.mjs (segmentation), cf.mjs
(counterfactual accepts), shadow.mjs / h5bucket.mjs (pre-reset backup shadow outcomes).

## 5. Shipped (2026-09-18, working tree — restart `pump-desk-main` to deploy)

### Real root cause of the H4 wall: a stale swap SDK, not sniping

Probing a *static* healthy 75-SOL pool with the old code, the program logs showed
`Left: 23,000,000 / Right: 24,674,391` — the on-chain buy needed ~23% more quote than
`@pump-fun/pump-swap-sdk` 1.18 quoted, so any bound under ~25% failed with 6004 with
zero price movement. 1.18 predates the program's `GetFeesWithQuoteMint` fee model
(quote-mint schedules, mayhem mode, configurable creator fee). **Upgraded to 1.20.0:
the same pool passes buy+sell at 2%.** The same defect explains the live entry
failures (5 → 10 → 25% all 6004 on 2026-09-17 04:53).

Replay of the new probe on 10 recent `price_moved` mints: 10/10 `pass`, ~140 ms,
855–1042 bytes with the ALT.

### Code

- `src/executor/pumpAmm.ts` — `buildProbeSwap()`: one `swapSolanaState` read for both
  legs; the sell moves the exact `base_amount_out` decoded from the buy ix
  (`decodeBuyBaseOut`, on-chain ABI), ending the InsufficientFunds false fails.
- `src/executor/sellability.ts` — bound from `guardrails.sellabilityProbeSlippagePct`
  (default 50; must stay < 100 — the SDK's sell min-out math breaks at ≥ 100);
  `probeLayout()` finds the swap ixs by Anchor discriminator (the SDK's optional
  `extendAccount` is also a PumpSwap ix); `classifySellabilityError(err, source,
  layout)` attributes InstructionErrors by leg (before buy → `account_setup_unavailable`,
  buy → `price_moved`/`buy_failed`, sell onward → `sell_failed`); `poolMovePct`
  recorded on every result.
- `src/guardrails/checks/pending.ts` — optional `guardrails.maxProbeMovePct` gate
  (reports `price_moved`, never tolerated). Off in config until a move-vs-outcome
  distribution exists.
- `src/guardrails/checks/indexed.ts` — **H11 `unindexed_mint`**: hard veto when the
  pool is present but mint, DAS metadata and token age are all unindexed (the 1–2 s
  bundled launches). Registered last in `CHECKS`.
- `config.yaml` — `sellabilityProbeSlippagePct: 50`; exits `tp0Enabled: false`,
  `tp1Pct: 15`, `tp1SellFraction: 1`, `relaxedRiskTp0Enabled: false`.
- Tests: 464/464 (was 452); typecheck clean.

### Exit policy: single +15% take-profit (replay on 674 paper paths)

| policy | avg %/trade | median | win% | censored |
|---|---|---|---|---|
| ladder TP0 22/50 + TP1 40/50 + trail 12/8 (old) | +4.96 | +2.8 | 65% | 7% |
| **TP +15 full exit + trail 12/8 backstop (new)** | **+6.32** | **+13.6** | **71%** | 4% |
| TP +10 full exit | +5.50 | +9.0 | 82% | 3% |
| TP 20 / 25 / 30 (pessimistic bound) | +6.2 / +5.8 / +5.6 | ~+3–4 | 67–68% | 33–52% |
| trail only 12/8 | +4.31 | +2.5 | 64% | 19% |

61% of paths reach +15% (median 18 s); 43% reach the old TP0 at +22%. Method:
tick paths from the pre-reset backup, emergency-exit truncations excluded,
historical FSM exit as the censor point, 1%/3% TP/stop fill haircut, paper fees.
Shadow tracking left off (operator choice).

### What to watch after restart

H4 pass becomes the majority on 60–80 SOL pools; `H11` ≈ the former LOW_SCORE-at-40
count; accepts > 0; entries land at 5–8% slippage; closes as `TAKE_PROFIT_1` ~+15%.
