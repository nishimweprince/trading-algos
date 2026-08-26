# Phase 3 post-S6/S7 scorecard

This scorecard incorporates S6's failed unseen-fold evidence and S7's descriptive PropGuard simulation. It does **not** overwrite `phase3-gate-scorecard.json`, select a production coordinate, or authorize paper/live trading.

## Decision

**Phase 3 redesign remains NOT authorized. Edge reality is failed descriptive evidence from S6; S7 remains insufficient for a prop-survivability claim. The original pre-redesign blocking scorecard is unchanged.**

Pass: **1 / 10**; fail: **4**; not yet testable: **5**.

Edge reality is **fail** on descriptive S6 evidence. Prop-survivability remains **not-yet-testable** because S7 cannot support a claim: 4–15 clusters per mode, partial M1, modeled costs/tails, conservative sizing, and a free-margin proxy.

## Run identity and data limits

| Field | Value |
|---|---|
| Original scorecard | `phase3-gate-scorecard.json` |
| Overwrites original | false |
| Symbol / timeframe | XAUUSD / M15 |
| Bars | 2000 |
| Candle fingerprint | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| Conservative fallback | `pessimistic_same_bar_no_subpath` |

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
| Is the edge real? | `s6-nested-walk-forward.json`: `aggregate_unseen.net_r / net_expectancy_r; folds[*].unseen_net_r; deflated_sharpe_ratio.probability; cscv.probability_of_backtest_overfitting` | unseen aggregate -1716.84 net pips / -5.8710 net R over 11 structures; 2/4 folds positive expectancy; 1/4 folds positive marked R | DSR probability 0.0305%; PBO 40.0000%; raw Sharpe -0.5170 | **fail** |
| Prop-survivable? | `s7-propguard-monte-carlo.json`: `modes[*].simulation.daily_limit_breaches / total_limit_breaches / minimum_free_margin_pct_distribution` | 0/8000 paths breached requested 3%/5% daily or 6%/10% total limits; cluster counts 4 to 15; 1st-percentile free-margin proxy 95.70 to 97.62% | daily 3% breach probability 0.00% to 0.00%; conditional times undefined because no path breached | **not-yet-testable** |

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

### Is the edge real? — fail

Gate: Positive in most unseen folds, no dominant session, broad plateau, DSR and PBO reported.

S6 supplies failed descriptive out-of-sample evidence: the rolling unseen aggregate is negative, most folds are not positive, and DSR/PBO are reported. This does not authorize redesign promotion; it changes edge reality from not-yet-testable to fail.

### Prop-survivable? — not-yet-testable

Gate: Monte Carlo breach probability must be comfortably below firm limits.

S7 is a complete descriptive harness, not a survivability claim: libraries contain only 4 to 15 clusters per mode, M1 is partial, costs and tails are modeled, sizing is conservative 0.1% equity per R, and free margin is a risk-budget proxy rather than broker margin. Multi-year clusters and broker execution/margin observations are still required.
