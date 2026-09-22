## Goal

Implement the fixes diagnosed in `h4.txt` for guardrail checks H4 (sellability), H5 (holder concentration), and H6 (creator holdings), in the recommended order: H5 data correctness first, then the H4 address lookup table (ALT) setup, then H5 cap calibration, leaving RPC index-lag unknowns on the existing tolerate-unknown path.

## Success Criteria

- H5 no longer counts the pre-migration bonding-curve holding (~20% of supply) in top-10 / largest-holder shares, regardless of `confirmed` vs `processed` snapshot timing.
- H5/H6 no longer fail with "could not find account" / "holders unavailable" when `getTokenSupply` lags while the mint account itself is readable (the 3.5 s lag mode from the diagnosis).
- H4 atomic buy+sell probes fit in the 1232-byte limit via the already-built lookup-table path (`src/executor/sellability.ts` + `guardrails.sellabilityLookupTableAddress`), so "atomic buy+sell simulated cleanly" becomes the majority outcome instead of 97% unknown.
- H5 cap reflects what real fresh graduations look like (ex-vault top-10 of 39–100% in the diagnosis), or H5 is explicitly re-scoped to a soft signal — an operator decision, not a silent veto-everything.
- `npm test` (affected suites) and `npm run typecheck` pass; live probes confirm each fix.

## Context And Current Facts

- Diagnosis (`h4.txt`, 2026-09-17, ~1,141 candidates / 24 h): H4 unknown 97% (probe tx 1255–1287 bytes > 1232-byte limit); H5 pass only 2.4% (stale snapshot +~20 pts plus an unattainable 25% cap); H6 unknown 50%, all "holders unavailable" collateral from H5's data path. Only 21 clean accepts, all with H5 = pass; 530+ hard-vetoed on an inflated H5 number.
- H4 infra is already built: `SellabilitySimulator` in [sellability.ts](/home/basis/trading-algos/pump-fun/src/executor/sellability.ts) loads `guardrails.sellabilityLookupTableAddress`, passes it through `assembleSignedSwapTx` in [assemble.ts](/home/basis/trading-algos/pump-fun/src/executor/assemble.ts) (`compileToV0Message` with `addressLookupTableAccounts`), and logs `txBytes` / `usedLookupTable` either way. The config key is commented out in [config.yaml](/home/basis/trading-algos/pump-fun/config.yaml) (~line 206). Buy-only backstop (`sellabilityBuyOnlyBackstop`) and the tolerate flags are already wired in [engine.ts](/home/basis/trading-algos/pump-fun/src/guardrails/engine.ts) (`canTolerateUnknown`, `price_moved` unconditionally excluded).
- H5 check ([pool.ts](/home/basis/trading-algos/pump-fun/src/guardrails/checks/pool.ts) `checkHolderConcentration`) excludes only `pool.baseVault` / `pool.quoteVault` plus `BURN_OWNERS` — no bonding-curve exclusion. The relaxed-risk mirror of this logic lives in `computeRelaxedReasons` in [engine.ts](/home/basis/trading-algos/pump-fun/src/guardrails/engine.ts) and must be updated in lockstep.
- Holder fetch ([holders.ts](/home/basis/trading-algos/pump-fun/src/enrichment/holders.ts) `fetchHolders`) resolves supply as DAS hint → `rpc.getTokenSupply(mint)`; the mint account (whose bytes already contain supply u64 LE at offset 36, decoded by `decodeMint` in [mint.ts](/home/basis/trading-algos/pump-fun/src/enrichment/mint.ts)) is fetched in parallel in [index.ts](/home/basis/trading-algos/pump-fun/src/enrichment/index.ts) `enrich()` but never shared with the holders path. `getTokenLargestAccounts` is pinned to `confirmed` in [rpc.ts](/home/basis/trading-algos/pump-fun/src/core/rpc.ts); `getTokenSupply` has no retry.
- ATA derivation helper already exists in [ata.ts](/home/basis/trading-algos/pump-fun/src/core/ata.ts) (`deriveAta`, `findProgramAddressSync` under the ATA program).
- Config constraints: `holdersNotMintRetryDelaysMs` must sum to less than `enrichmentBudgetMs` ([schema.ts](/home/basis/trading-algos/pump-fun/src/config/schema.ts)); live budget is 2500 ms with retries `[0, 300, 700, 1200]`. `config.yaml` WS10 comment says "25 -> 35 after EV positive" — any cap move must update that note and the `strictTop10HolderCapPct` relaxed tagging.
- H6 (`checkCreatorHoldings`) needs no logic change: with holders present it already passes/fails on real numbers (456 pass / 113 fail per diagnosis).

## Constraints And Non-goals

- No change to the 2.5 s enrichment budget or to the tolerate-unknown policy: 10 s+ index-lag mints stay `unknown` via `tolerateUnknownWhenNoHardFail` (diagnosis action d). Do not stretch the budget or add long retries to chase them.
- Do not retry `getTokenSupply` — it is removed from the path, not hardened.
- Do not change H4 probe semantics (atomic-first, buy-only backstop gating, `price_moved` never tolerated). The ALT work is setup + config, not probe-logic changes.
- No unrelated threshold or sizing changes beyond the H5 cap decision.
- The ALT creation signs and sends a real transaction from the trading wallet (~0.002 SOL rent): needs explicit operator approval, never done silently as part of a code change.

## Key Decisions

- **Bonding-curve exclusion by derived ATA account AND owner (belt-and-braces).** Exclude the bonding curve's associated token account (derived locally via `findProgramAddress` + `deriveAta`, zero RPC) from the H5 "real holders" filter, and also skip any holder whose resolved `owner` equals the bonding-curve PDA. Reason: the account-address exclusion handles the exact stale-snapshot case; the owner exclusion covers ATA-shape surprises. Alternative rejected: switching the snapshot to `processed` — reduces but does not eliminate the 1–2 slot race, and touches shared RPC semantics.
- **Supply priority: mintInfo > DAS hint > `getTokenSupply` last-resort fallback.** `enrich()` already reads the mint account; share that promise with `fetchHolders` (same promise-sharing pattern already used for the DAS hint) instead of sequencing the two fetches serially. Keep `getTokenSupply` only as a fallback for when `mintInfo` itself is missing, so behavior never regresses where the mint read fails. Alternative rejected: parsing supply inside `holders.ts` via a second `getAccountInfo` — duplicates the mint read and costs an extra RPC per candidate.
- **ALT member list derived from a real assembled probe, not hand-enumerated.** Build the ALT creation script so it assembles one buy+sell probe (via the existing `PumpAmmClient` builders), collects the static/repeated addresses (program ids, fee accounts, event authority, token/ATA/system programs, WSOL mint), and creates + extends the ALT with exactly that set. Alternative rejected: hardcoding the address list — drifts the moment the SDK's `remaining_accounts` change (the SDK already varies them per transaction).
- **H5 cap is an operator calibration, default recommendation: raise toward observed reality.** Evidence says ex-vault top-10 on real graduations is 39–100% and even 35% (the WS10 note) still fails most. Recommend the operator pick in the 40–60% band or re-scope H5-at-graduation to a soft score input; keep `singleHolderCapPct: 8` tight (single-dumper protection stays). Do not pick the number in code — it ships as a config change with the WS10 comment updated.
- **H6 needs no code change.** Fixing H5 actions (a) and (b) fixes H6's unknowns automatically; real creator-hold fails (33%/79% cases) keep vetoing.

## Recommended Approach

Two small code changes (H5a exclusion, H5b supply source) with unit tests, then an operator-approved ALT setup runbook (script + config flip + live confirmation), then a one-line operator config decision on the H5 cap. Each phase validates against the repo suites plus a live-probe rerun before the next begins. Total code surface: `src/guardrails/checks/pool.ts`, `src/guardrails/engine.ts` (relaxed mirror), `src/enrichment/holders.ts`, `src/enrichment/index.ts`, one new `scripts/`-style ALT CLI (or npm script entry), plus tests.

## Work Plan

1. **H5a — exclude the bonding-curve holding from H5 (code, small).**
   - Derive the bonding-curve PDA from the mint with `findProgramAddressSync` under `PROGRAM_IDS.PUMP_FUN`, then its ATA with the existing `deriveAta` helper (respect `mintInfo.isToken2022`).
   - In `checkHolderConcentration` ([pool.ts](/home/basis/trading-algos/pump-fun/src/guardrails/checks/pool.ts)), exclude that ATA account alongside `pool.baseVault` / `pool.quoteVault`, and also skip holders whose `owner` equals the bonding-curve PDA. Needs the mint + token-program identity in the check context (via `candidate.enrichment.mintInfo` / graduation mint); if the curve address cannot be derived, fall back to current behavior (no new unknown).
   - Mirror the exclusion in `computeRelaxedReasons` ([engine.ts](/home/basis/trading-algos/pump-fun/src/guardrails/engine.ts)) so relaxed tagging agrees with the hard check.
   - Unit tests: stale-snapshot fixture (top holder = curve ATA at ~20%, vault excluded) now passes with the same top-10 the diagnosis measured 4 slots later (78.9% → ~59.7%); curve-ATA-absent fixture behaves as before.
   - Depends on: confirming the bonding-curve PDA seeds against one live migration (see Open Questions) before merging.
2. **H5b — supply from the already-read mint account (code, small).**
   - In [index.ts](/home/basis/trading-algos/pump-fun/src/enrichment/index.ts), share the in-flight `mintInfo` promise with `fetchHolders` as a `SupplyHint` (supply = `decodeMint` u64 at offset 36, decimals byte at 44 — already decoded and tested in [mint.ts](/home/basis/trading-algos/pump-fun/src/enrichment/mint.ts)).
   - In [holders.ts](/home/basis/trading-algos/pump-fun/src/enrichment/holders.ts), prefer that hint over `getTokenSupply`; keep DAS hint and `getTokenSupply` as ordered fallbacks (mintInfo > DAS > RPC) so a missing mint read never newly unknowns H5/H6.
   - Preserve parallelism: pass the promise (as with the existing DAS-hint race), do not await mint before starting `getTokenLargestAccounts`.
   - Unit tests: mint-readable + `getTokenSupply`-lagging fixture yields correct shares with zero `getTokenSupply` calls; mint-missing fixture still falls back to `getTokenSupply`.
   - Depends on: nothing (parallelizable with phase 1); ship together as the "H5 data correctness" unit.
3. **H4 — ALT creation script + config flip (ops, needs wallet-tx approval).**
   - Add a small one-shot CLI (e.g. `npm run alt:create`) that: assembles one representative buy+sell probe with `PumpAmmClient.buildBuy`/`buildSell`, extracts static addresses, creates an ALT (`AddressLookupTableProgram.createLookupTable`), extends it with the static set, waits for activation, and prints the address. Reuse `assembleSignedSwapTx` byte-count logging to show before/after sizes.
   - Operator runbook: review the member list, approve the wallet signature (~0.002 SOL rent), set `guardrails.sellabilityLookupTableAddress: <ALT>` in [config.yaml](/home/basis/trading-algos/pump-fun/config.yaml) (uncomment ~line 206), restart (no hot-reload).
   - Confirm live: H4 "atomic buy+sell simulated cleanly" becomes the majority; `usedLookupTable: true` in logs; tx bytes < 1232.
   - Depends on: phases 1–2 merged (so H4 confirmation isn't confounded by H5 unknowns). Code risk is nil — the lookup path already exists and is covered by [sellability.test.ts](/home/basis/trading-algos/pump-fun/test/sellability.test.ts).
4. **H5 cap calibration (operator decision, config-only).**
   - With (a)+(b) live, re-measure ex-vault top-10 on real graduations and set `top10HolderCapPct` to the chosen band (diagnosis suggests 40–60% is what actual graduations look like), or re-scope H5-at-graduation to a soft score input instead of a hard veto. Update the WS10 "25 -> 35" comment, `strictTop10HolderCapPct` tagging, and dashboard snapshot if thresholds move.
   - Depends on: phases 1–3 (measuring on corrected numbers with H4 healthy).
5. **Leave index-lag unknowns alone (no work).**
   - Keep `tolerateUnknownWhenNoHardFail` as the path for 10 s+ late-indexing mints; no budget or retry-schedule change (schema constraint would need re-validation anyway). Verify the veto breakdown shows residual H5/H6 unknowns are the late-index tail, not the fixed modes.

## Validation Plan

- **Phases 1–2 (code):** `npx vitest run test/guardrails.test.ts test/enrichment.test.ts test/sellability.test.ts` — must pass unmodified plus the new fixtures; then full `npm test` and `npm run typecheck`. Highest-risk step: the bonding-curve PDA seed confirmation — a wrong seed silently excludes nothing (safe direction: current behavior) but also fixes nothing, so the live check below is the real gate.
- **Phase 1 live:** rerun the `holdersprobe.ts` scratchpad pattern on live migrations — same mint at migration slot vs +4 slots must now agree within noise (the EKMAPNEF 78.9% → 59.7% case collapses), and the "top10 100.0%" curve-stake cases drop out of the fail set.
- **Phase 2 live:** rerun the `indexlag.ts` pattern — `getAccountInfo(mint)`-readable + `getTokenSupply`-lagging mints now enrich H5/H6 successfully; assert via logs that `getTokenSupply` is no longer on the hot path when `mintInfo` is present.
- **Phase 3 live:** after the ALT flip, confirm majority H4 pass with `usedLookupTable: true` and assembled bytes < 1232; the ~530 "buy leg clean" and ~360 "backstop inconclusive" buckets should collapse into atomic passes (genuine `price_moved`/`sell_failed` still veto/unknown as before).
- **Phase 4:** re-measure ex-vault top-10 distribution over ≥ 1 day post-fix before locking the cap number.

## Risks / Rollback

- **Wrong bonding-curve seeds** → exclusion silently no-ops (falls back to today's behavior; no new vetoes). Mitigation: live-verify the derived ATA against one observed stale snapshot before merge. Rollback: revert the exclusion lines; H5 returns to current behavior.
- **Mint-derived supply wrong on Token-2022 extensions** → shares miscomputed. Mitigation: `decodeMint` already handles both layouts with tests; keep `getTokenSupply` fallback and gate on successful decode only. Rollback: restore RPC-first supply order.
- **ALT contains a wrong/incomplete member set** → probe still overflows, H4 stays on the backstop path (today's behavior, relaxed-risk). Mitigation: log before/after byte counts from the script; verify < 1232 before the config flip. Rollback: comment out `sellabilityLookupTableAddress`, restart.
- **Raising the H5 cap admits real concentration risk.** Mitigation: keep `singleHolderCapPct: 8` and creator cap tight; ship the cap move as its own config change after measured data; relaxed-risk sizing still caps widened-threshold accepts.
- **ALT creation spends real SOL and needs the trading wallet.** Never run without explicit operator approval; dry-run the member list offline first.

## Open Questions

- **Bonding-curve PDA seeds:** assumed `["bonding-curve", mint]` under the pump.fun program (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) — must confirm against one live migration's observed curve account before merging phase 1. If the seeds differ, the fix is the same shape with corrected seeds.
- **H5 cap value / veto-vs-soft-signal:** operator call after re-measurement (40–60% band per current evidence, or soft-signal re-scope). No code ships until the operator picks.
- **ALT member finalization:** exact static set comes out of the script's probe assembly against the current SDK; the plan does not hardcode it because `remaining_accounts` vary per transaction.

## Execution Notes (2026-09-17, approved build)

- **Bonding-curve seeds VERIFIED** (was an open question): `["bonding-curve", mint]` under `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` matched stored mainnet enrichments — mint `CZ2e...pump` curve `NjRA...` owns the 20.69% top holder; same pattern on `Fwiq...pump`. The stale holder matches by **owner**, not by derived ATA, so the owner match does the real work.
- **Shipped phases 1–2:** `src/enrichment/curve.ts` (new), H5 exclusion in `src/guardrails/checks/pool.ts` + relaxed mirror in `src/guardrails/engine.ts`, mint-supply hint in `src/enrichment/index.ts` + multi-source `resolveSupplyHint` in `src/enrichment/holders.ts`. Full suite 452 passed, typecheck clean.
- **Live-data check on shipped code:** `CZ2e` raw top10 46.3% → 27.8% (18.5 pts inflation removed); `Fwiq` 78.9% → 61.0% (17.9 pts). Both still fail the 25% cap — confirms phase 4 (operator cap calibration) is still required.
- **Shipped phase 3 tooling:** `src/executor/alt-setup-cli.ts` + `npm run alt:setup` (dry run default; `--execute` sends 2 txs). Member selection extracted to `src/executor/altMembers.ts` with `test/altMembers.test.ts`. **NOT run:** the `--execute` wallet transaction and config flip await explicit operator approval.
- **Phase 4 NOT done:** `top10HolderCapPct` value / veto-vs-soft-signal is an operator decision after re-measurement.
