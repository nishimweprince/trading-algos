# Racer follow-ups after the entry-quality repair

Branch: `feat/migration-racer`. Status: implemented and deployed racer-only on Frankfurt (2026-10-05); soak running toward 300 verified +1 mid tracks. The racer remains paper-only; no wallet, signer, or strategy selection is added here.

## Baseline and decision gate

The admission-time vault watch, missing-anchor null entries, versioned entry quality, verified-only summaries, and observed same-slot depletion cohorts are the measurement baseline. Pre-repair rows remain `legacy-unverified`; do not backfill them from reach. Reassess profitability only after at least 300 **verified +1 mid** tracks, with a separately reported count of every migration that could not produce one. Predeclare cohort and exit rules before inspecting their returns; reserve a later time window for validation.

## 1. Audit coverage and thin pools

- Reconcile every migration into: verified track, late-armed/missing anchor, thin pool, non-WSOL quote, or another explicit reason. Display counts and percentages over all migrations and over priced tracks.
- Audit `thin-pool` against the recorded migration transaction and initial quote-vault balance. Save the observed initial quote depth and configured threshold, so the label can be reproduced without a later RPC read.
- Test boundary cases at the threshold and mismatched quote mints. Do not infer tradeability from the currently selected tracks alone.

## 2. Capture selection features available at entry

- Persist an immutable feature snapshot at the proposed +1 mid entry: fire-to-complete and complete-to-migrate slots, migration caller/cohort, quote depth, Token-2022 flag, migrate-slot token depletion, observed S and S+1 buyer count/volume up to that entry, and failed migrate attempts known by then.
- Distinguish a feature that was known before submission from one learned after the entry. Later S+1 activity is diagnostic only, not a selector.
- Add verified-only dashboard splits with sample sizes and uncertainty, including results with and without high same-slot depletion. Avoid choosing thresholds by maximizing the same sample's P&L.

## 3. Model landing and fixed-size capital accurately

- Count S+1 pool buy attempts and failures from the transaction stream. Separate on-chain failures from transactions that never reached the chain, which this feed cannot observe. Model missed entry as no position, not as a loss or a guaranteed filled trade.
- Recalculate paper fees, tips, impact, and fixed costs at a configurable proposed SOL size. Show cumulative fixed-size paper P&L, capital concurrently committed, and ATA rent as locked capital; never label these as a compounded $25 wallet balance.
- Test that failures and unavailable entry prices cannot contribute a synthetic win; disclose the unknown off-chain drop rate as a sensitivity range.

## 4. Path-based exits, then out-of-sample validation

- Retain ordered vault states through the longest horizon and simulate fills at the next observable state after a stop-loss, take-profit, trailing stop, or time exit. Include sell impact, fee/tip, quote-vault cap, and rugs. Missing states must produce an explicit unpriced exit, not a favorable mark.
- Replay fixtures covering intra-slot moves, gaps through stops, simultaneous triggers, a rug, and a depleted quote vault. Compare each rule against the existing 30/60/300 s exits on the same verified entries.
- Freeze candidate rules and selection thresholds, then evaluate on a later, untouched set. Report mean, median, win rate, mean without best trade, bootstrap interval, worst loss, coverage, and results both including and excluding the high-depletion cohort. A positive in-sample result alone does not authorize live trading.

## Ordering

Coverage audit and entry-time feature snapshots first; landing/size accounting next; path exits after the vault-state capture is reliable. Keep the dashboard's legacy/unverified exclusions and cohort denominator consistent throughout. No live trading or strategy deployment is part of this plan.

## Implementation notes

- Coverage now reconciles migration rows to verified, unverified, missing-track, or explicit unpriced reasons. Thin-pool rows retain reconstructed opening depth when available, observed post-migrate depth, and the threshold used by the gate. Missing depletion is its own cohort.
- Decision-time features are snapshotted on receipt of the landed migrate transaction. S/S+1 pool-buy volume and recipient-owner counts are later diagnostics only. The stream cannot observe off-chain drops; its on-chain failure rate is labelled a proxy.
- The 0.1 SOL cumulative curve is fixed-size paper P&L, not wallet equity. The proposed-size scenario reprices only rows with landed-exit depth; unavailable reprices stay unavailable. ATA rent is counted as locked capital.
- Path rules are fixed at −20% stop, +30% take, 15-percentage-point trailing stop, a combined rule, and a time exit at the longest configured hold. Trigger marks do not serve as fills. A path missing a migration-slot base or quote anchor remains unpriced. A missing following state is NOT missing data on a continuous stream (both vaults are watched through the hold, so no write means no trade): the fill is the unchanged last state, or the entry state at the deadline, counted as `fill: unchanged`. Only a stream gap (reconnect, SDK stream error, failed watch update) during the hold leaves such an exit unpriced — dropping quiet or crashed pools biased results upward. The first 300 completed verified tracks are the exploratory set; later tracks are reserved and no rule is marked validated.
