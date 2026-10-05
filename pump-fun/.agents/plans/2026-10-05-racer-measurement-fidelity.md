# Racer measurement fidelity — pre-deploy plan

Date: 2026-10-05. Branch: `feat/migration-racer`. Status: **Draft**.
Inputs: `migration-racer-spec.md`, `reports/migrate-racer-verdict.md`, `.agents/plans/2026-10-05-migration-racer.md`.

## Decisions (operator, 2026-10-05)

- Racer is **not deployed yet**; fix before the first Frankfurt start.
- Goal is **measurement fidelity**, still zero sends. No migrate builder, no bundles, no live.
- Curve state comes from a **LaserStream account subscription** (revises D4: same provider, same box, no new feed).
- Top-caller (`9C4n…`, 46%) identification is **out of scope**; follow-up only.

## Audit: spec vs. build

| Spec item | Built | Status |
| --- | --- | --- |
| §7 verify R from chain data | `research/migrateVerify.ts`, 1000 migrates, verdict R=0 | Done |
| §7 checklist in spec | Boxes still unchecked | Docs gap |
| Pipeline: fast feed → near-complete watcher | RPC polling watcher, admission at launch time | **Broken** (G1) |
| Pipeline: fire gate (cost < payout) | `evaluateFire` vs fixed SOL ceiling (R=0) | OK as adapted |
| Pipeline: atomic migrate + buy bundle | Absent by verdict (no maintained builder) | Deliberate |
| §8 measure net, not gross | Paper PnL with flat 1% fee, entry after racers | **Biased** (G2, G5) |
| §8 landing rate / tip sizing | Not measurable without sends | Deliberate; proxy in G4 |
| Plan "done means": deploy + report | pm2 entry only; no deploy report; README silent on racer | Open |

## Gaps

- **G1 (critical) Admission never admits.** `racer-worker.ts` reads each curve once when its launch row lands. A fresh curve costs ~85 SOL to complete; `admitCostSol` is 5. Nearly every mint is rejected and never re-read. The admission path has no test.
- **G2 First position is not measured.** Paper entry is the first RPC-readable pool price after we notice `complete`, which is after the migrator's bundled buys. "Does first position pay" needs the pool's opening price.
- **G3 Mislabelled migrations.** `complete` flips on the last buy; `migrate` follows 0–3 slots later (preArm measurement). `readPool` runs immediately, so a normal migration can be logged `complete-no-pool` and loses its paper track.
- **G4 Lead time measures our poll lag.** `leadSlots` = our next tick's `getSlot` minus our fire tick's `getSlot`. It does not give would-fire → complete → migrate in chain slots.
- **G5 Paper cost model is flat.** 1% round trip, no price impact, no PumpSwap fee tier, no pump.fun fee on the completion buy.
- **G6 Throughput.** 200 tracked × one HTTP read each, 4 concurrent, 900 ms timeout, every 2 s: ticks overrun and get skipped.
- **G7 Ops gaps.** No racer section in `deploy/fra/README.md`; plan D1's "own wallet" is moot for zero-send (ephemeral unsigned payer); no soak exit criteria written down.

## Plan

### Step 1 — Stream curve state (fixes G1, G6)

- New `racer/curveStream.ts`, modelled on `guardrails/preArm.ts`'s account subscription: owner = pump.fun, memcmp discriminator @0, `complete=0` @48, and **`realTokenReserves` top 3 bytes = 0** (bytes 29–31). That pushes only curves with < 2^40 raw tokens left (~1.1M tokens, ≈0.45 SOL to finish at end-of-curve price), which is the would-fire zone with no polling.
  - Spike first: confirm the byte bound against `completionCostSol` on 20 live curves. If the zone is too narrow for useful lead time, add a second, wider filter (top 2 bytes + `realSolReserves` high byte), or fall back to the current RPC poller for the admit band only.
- Keep the existing `complete=1` subscription shape for the complete flip.
- Curve → mint: launches table lookup (as preArm does); unknown curves resolved by one `getTransaction` on the update's signature, cached.
- Each event carries the **update slot**, not a tick slot.
- Delete launch-time admission. The watcher becomes event-driven; `pollMs`/`maxConcurrent`/`admitCostSol` are retired or kept only for the fallback.
- Separate LaserStream connection under the racer's own Helius key (D1). Check the plan's stream-count limit before deploy.

### Step 2 — Real migration events (fixes G3, G4)

- On entering the would-fire zone, add the mint's canonical pool PDA to the racer's account subscription (same trick as preArm).
- Subscribe to pump.fun migrate transactions **including failed ones** (`failed: true`, migration authority required, `MIGRATE_LOG` match). Record per migration: winning caller, slot, and whether the winning tx also buys. Record every failed attempt in that slot window.
- Emit `migrated` only on a successful migrate tx (or the first pool write); `complete-no-pool` only after N slots with neither.
- New timing fields, all chain slots: `fire_slot`, `complete_slot`, `migrate_slot`, `failed_attempts`. Dashboard shows fire→complete and complete→migrate distributions (p50/p90), not one average.
- Failed attempts per migration is the zero-send proxy for competition intensity (`C_failed` in the spec's equation).

### Step 3 — Honest first-position pricing (fixes G2, G5)

- Record two entry prices per migration:
  - **open**: pool reserves as created by migrate, before any buy (from the migrate tx's pool-creation data, or the pool's first write when that tx has no bundled buy). This is what "be the migration" would get.
  - **first-reachable**: pool state after the migrate tx, which is the best a non-migrator can get.
- Horizons price from the pool PDA pushes already subscribed in step 2; RPC only as fallback.
- Paper PnL uses `positions/ammImpact.ts` + the PumpSwap fee tier model for `paperSizeSol`, plus the pump.fun fee on the completion buy cost. Drop the flat 1%.

### Step 4 — Tests

- Admission/stream: a fresh curve update is ignored; an update entering the byte band fires once; a re-entry after a sell does not re-fire.
- Labelling: complete then migrate 3 slots later gives `migrated`; complete with no migrate within N slots gives `complete-no-pool`.
- Pricing: open vs first-reachable on a fixture migrate tx with a bundled buy.
- Replay one recorded hour of LaserStream frames through the worker; event counts match a chain-side count of migrations in that window.

### Step 5 — Deploy and soak

- Add a racer section to `deploy/fra/README.md`: process, role, Helius key env name, no wallet, health checks (stream connected, events/hour, last migrate seen).
- Local soak 2 h on the Mac against mainnet before Frankfurt.
- Frankfurt dry-run soak until **≥ 300 migrations** (spec §8 "a few hundred"). Exit report answers:
  1. fire→complete and complete→migrate slot distributions;
  2. failed migrate attempts per migration;
  3. share of migrations whose winning tx bundles a buy;
  4. paper PnL at open vs first-reachable, after impact + fees, per horizon.
- Write `reports/racer-soak-<date>.md` and tick the plan's done-means boxes and spec §7 checklist.

## Not in this plan

Migrate instruction builder, bundles, tips, durable nonce, live wallet, identifying `9C4n…`.
