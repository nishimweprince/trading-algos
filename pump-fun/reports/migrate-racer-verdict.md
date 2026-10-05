# Migration-racer verdict (spec §7 + build decision)

Source: `reports/migrate-racer-verification.json` / `.md` (1000 migrates, 281 callers, 5 probes, 2026-10-04).

## R: zero

- Migrates with caller net-of-fee > 0: **0/1000**. No SOL flows to the `migrate` caller.
- Caller-owned token gains appear only as **buys bundled in the migrate tx**
  (positive token deltas paired with WSOL spend, e.g. −57.5 WSOL over 24 migrates
  for one caller) — spending, not rewards. No LP-token or payout-token reward observed.
- Top caller won **464/1000 (46%)** and paid **−1.0 SOL total (~0.002/migrate in tips)**,
  with zero buy/claim activity in its own wallet history (100 txs probed).

## Hypothesis ruling

| Hypothesis | Verdict |
| --- | --- |
| Caller reward on migrate | **RULED OUT** (0/1000 net-positive) |
| Push-over-the-line completion buys | **CONFIRMED** (13 same-slot buys in probes) |
| First position on new pool | **STRUCTURE CONFIRMED** (in-tx bundled buys), **profitability UNPROVEN** |
| Volume incentives | No evidence (0 claims in ~500 probed txs); inconclusive-leaning-negative |
| Paid entries / creator fees | Not tested (needed his wallets; never shared) |

The "$10 for losing $3" mechanism is **not** an on-chain payout to the `migrate`
caller. Racers pay to win, not the reverse. The top caller's motive is unexplained
by its own wallet (possible sibling-wallet profit in the same bundles, or a
non-trading keeper) — flagged as follow-up analysis, not a build gate.

## Build decision (plan step 4)

Caller-reward racer: **NO-GO** — nothing to collect.
Be-the-migration first-position variant: **CONDITIONAL GO**, gated on a buildable
`migrate` instruction path (spike below). Dry-run only per D3; the dry-run itself
is the experiment that measures detection timing, sim success, and first-position
paper profitability before any live discussion.

## Migrate-builder spike (blocking the build)

Repo rule S3a (sim-proven, `src/executor/curve.ts`): instruction bytes come from a
maintained builder, never hand-rolled. No migrate builder exists in the repo.
Spike, in order:

1. Maintained builder with migrate support (pump SDK / trade-local action) → use it.
2. Else empirical replication: ≥3 sampled migrate txs show a stable ix layout with
   deterministic accounts + caller → replicate behind the Broadcaster sim gate,
   dry-run only (zero sends; documented S3a exception).
3. Else STOP: no buildable racer this phase; report back with the cheaper alternative
   (faster post-migration entry on the existing scalper via LaserStream).

## Spike outcome (2026-10-05)

1. **No maintained builder.** `pumpdotfun-sdk@1.4.2` IDL has no migrate ix
   (initialize/setParams/create/buy/sell/withdraw only); PumpPortal trade-local
   rejects `action: "migrate"` (HTTP 400).
2. **Layout stable but hand-rolling foreclosed.** 2/2 direct-call samples show an
   identical 29-account layout with the same 8-byte discriminator and fixed
   program/sysvar positions. A 3rd sample calls migrate via CPI through wrapper
   program `AdBMsmwrHEAWrMZFkTiZhZpK1gHBCvDnD1S8YEtRbdLE` — the on-chain surface
   is already heterogeneous (direct + CPI). Per repo rule S3a, hand-rolling a
   29-account evolving surface is how money is lost; no S3a exception: the
   dry-run cannot measure wins/landings/tips without sends, so a replicated ix
   buys nothing but rot risk.
3. **Pivot, not stop.** Build the no-regret measurement half: near-complete
   watcher + completion-buy-leg sim (trade-local builds buys — maintained) +
   first-position paper tracker + metrics + deploy. This answers the remaining
   open questions (lead time, winner tip economics, does-first-position-pay)
   with zero sends. The migrate-bundle leg waits for a maintained builder.
