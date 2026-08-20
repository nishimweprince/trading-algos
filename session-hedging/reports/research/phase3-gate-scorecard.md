# Phase 3 gate scorecard

This scorecard evaluates the pre-written §9 gates from the already-committed research artifacts. It does not select an argmax, tune a parameter, or re-window the candle set.

## Decision

**Phase 3 redesign is NOT authorized; run S6 and S7 against the incumbent four modes.**

Pass: **1 / 10**; fail: **3**; not yet testable: **6**.

## Run identity and data limits

| Field | Value |
|---|---|
| Symbol / timeframe | XAUUSD / M15 |
| Bars | 2000 |
| Date bounds | 2026-07-21T05:45:00Z to 2026-08-19T23:30:00Z |
| Candle fingerprint | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| M1 coverage | partial: 93 / 2000 parent bars (4.65%) |
| M1 chronology used | no |
| Conservative fallback | `pessimistic_same_bar_no_subpath` |

The 2,000-bar M15 cache covers roughly 30 days of one symbol. It is sufficient to verify the harness and describe behavior; it is not sufficient for walk-forward selection or a prop-survivability claim. Those require multiple years of contiguous M15, covering M1, and broker bid/ask, slippage, commission, swap, margin, and gap observations across varied regimes. Partial M1 chronology is not mixed: the full window uses the conservative `pessimistic_same_bar_no_subpath` fallback.

## Every §9 gate

| Question | Artifact and field | Measured value | Interval | Verdict |
|---|---|---|---|---|
| Are signals where they claim to be? | `s3-anchor-study.json`: `cells[*].anchor_drift_p50 / anchor_drift_max; shared_params.anchor_tolerance_minutes` | 9/9 variants within tolerance; worst p50 10.0 min | p50 range 0.0 to 10.0 min; max observed drift 10.0 min | **pass** |
| Does the TP rate clear its bar? | `s8-scale-decomposition.json`: `cells[*].tp_rate_margin_pp / tp_rate_margin_pp_ci_low / tp_rate_margin_pp_ci_high` | 12/256 cells clear the lower-bound gate; 244/256 do not | margin -25.00 to 12.00 pp; CI lower bound -30.89 to 1.68 pp | **fail** |
| What scale? | `s8-scale-decomposition.json`: `cells[*].entry_mode / orb_minutes / entry_delay_minutes / max_age_hours / net_r` | 120/256 cells have positive net R; 112 distinct effective configurations | net R range -46.1556 to 14.8187 | **not-yet-testable** |
| Does the hedge earn two extra transaction sides? | `s4-cost-sensitivity.json; s2-break-frequency.json`: `cells[mode,cost].net_expectancy_pips / transaction_sides; cells[all,24h].double_break_rate` | at 2 pips/side hedge 7.16 vs synthetic 10.91 net pips/structure; hedge uses 194 vs 75 sides | 2-4 pips/side: hedge 7.16 to -0.84, synthetic 10.91 to 6.91; 24h double-break 56.9% | **fail** |
| Enough cost headroom? | `s4-cost-sensitivity.json`: `cells[*].breakeven_pips_per_completed_side / cost_headroom_ratio` | hedge break-even 3.79 pips/side; headroom 1.90x at 2 pips and 0.95x at 4 pips | modeled spread interval 2 to 4 pips/side; no broker-measured interval | **fail** |
| What RR? | `s1-conditional-target-hit.json`: `reach_cells[all,24h,3R].unconditional` | 24h conditional 3R reach 30.6% (11/36) | 95% CI 18.0% to 46.9% | **not-yet-testable** |
| Does the lock help? | `s1-conditional-target-hit.json`: `conditioning / reach_cells[*].lock_survived` | lock touched by 24 of 36 conditioned survivors | no walk-forward LOCK_MODE interval exists | **not-yet-testable** |
| Holding horizon? | `s8-scale-decomposition.json`: `cells[*].hold_buckets[*].gross_r / net_r / structures` | [0h,8h] -1713.8655 net R; (12h,24h] 497.6400 net R | five exhaustive buckets from [0h,8h] through (48h,+inf) | **not-yet-testable** |
| Is the edge real? | `s8-scale-decomposition.json; s9-regime-attribution.json`: `cells[*].net_r; flags[*].reason` | no unseen folds, DSR, or PBO; 3 directional concentration flags | S8 net R -46.1556 to 14.8187 in-sample only | **not-yet-testable** |
| Prop-survivable? | `s8-scale-decomposition.json`: `cells[*].prop_guard_breached / prop_guard_breach_events` | 0/256 deterministic short-window cells breached PropGuard; S7 not run | no Monte Carlo confidence interval or tail distribution exists | **not-yet-testable** |

## Gate rationale

### Are signals where they claim to be? — pass

Gate: Every configuration must have p50 drift <= 15 minutes.

No S3 anchor variant is void under the pre-written tolerance gate.

### Does the TP rate clear its bar? — fail

Gate: The lower confidence bound of TP-rate margin must be above zero.

Passing cells are a sparse subset of an unfrozen 256-cell in-sample surface; promoting them would be forbidden post-hoc selection, not a gate pass.

### What scale? — not-yet-testable

Gate: Use the complete same-window surface and prefer a broad plateau over a peak.

The complete surface exists, but 2,000 M15 bars (about 30 days) cannot support selection and the delay grid contains 144 duplicates by construction.

### Does the hedge earn two extra transaction sides? — fail

Gate: The hedge must repay its extra sides in net expectancy or breach probability.

The hedge has lower modeled net expectancy throughout the realistic cost interval and no S7 evidence of a compensating breach-probability reduction.

### Enough cost headroom? — fail

Gate: Break-even pips/side must be at least 2x measured broker spread.

The hedge misses 2x even at the low end, and the gate explicitly requires measured broker spread rather than the modeled S4 ladder.

### What RR? — not-yet-testable

Gate: Use conditional hit rates crossed with MAX_AGE_HOURS, not a single sweep.

The interval spans the break-even requirement and the one-month sample cannot select RR jointly with holding horizon.

### Does the lock help? — not-yet-testable

Gate: Compare LOCK_MODE values under walk-forward.

S1 describes the incumbent absolute lock; it does not compare lock modes out of sample.

### Holding horizon? — not-yet-testable

Gate: Confirm the 24h prior out of sample and price swap.

The descriptive attribution supports studying longer holds, but it is in-sample and financing cost is zero rather than broker-measured.

### Is the edge real? — not-yet-testable

Gate: Positive in most unseen folds, no dominant session, broad plateau, DSR and PBO reported.

S6 has not run, and S9 shows that three modes draw at least 75% of surviving winners from the long side on a strongly rising month.

### Prop-survivable? — not-yet-testable

Gate: Monte Carlo breach probability must be comfortably below firm limits.

A one-month deterministic replay does not test clustered losses, gap tails, spread tails, concurrent exposure, or time to target.
