# Pre-registration: Helix holdout (PLAN 0.3, 3.3)

Written 2026-10-07, after the discovery sweeps and **before any holdout number
was computed**. `research.helix_sweeps --split holdout` refuses to run until this
file exists, and this file is committed first.

## Days (`research/data/helix_inventory.json`)

| Role | Days | Used for |
|---|---|---|
| Discovery | 2026-08-10 → 09-17, 39 days (TWAP regime; 08-07..09 have < 20 h of Kalshi windows) | choosing everything below |
| **Holdout** | 2026-09-18 → 09-30, 13 days (the last 14 TWAP days; 10-01 has 7 h of Kalshi) | the tests below, run once |
| Earlier regime | 2026-07-27 → 08-06, 11 days (spot settlement) | reported only, never sets a parameter |

Settlement: Polymarket's own outcome (Gamma). Fee: 0.07 × p(1 − p) per share (every window).

## Discovery numbers these rest on (`helix_sweeps_discovery.json`)

- Base rule (gap 0.10, cap 0.60, 20/60 s): 12,843 entries, **+5.78¢/share at the touch**
  (s.e. 0.43), day-t 9.1, 36/39 days and 7/7 assets positive.
- Null battery: `signal` edge +3.03 pp, `random_same_rule` −0.80 pp: **excess 3.82 pp**
  (s.e. 0.39).
- Price side at 150 ms (an entry counts if the cheap ask L ms later is at or below the
  limit, net at that ask): base fill share 53%, **+2.44¢ per entry** at the touch limit,
  **+2.98¢** at touch + 2¢. Per day (one share per entry): base 805¢, t15 890¢,
  t20 1,058¢; with +2¢: 982, 1,096, 1,262.

## Gate on the holdout (README §Gates, null battery)

- **N1.** `signal` − `random_same_rule` edge ≥ **1.9 pp** (half the discovery excess,
  rounded down: winner's curse is assumed to take up to half), and the excess
  positive at ≥ 2 s.e. If not met, the signal is not shown to be real on this tape.
- **N2.** Base rule at the touch: net ¢/share > 0 with day-t ≥ 2 over the 13 days.

## Comparisons (at most two promotions, PLAN 3.3)

Each is a paired comparison over the same holdout windows, at 150 ms, price side.
A change is promoted only if its holdout day-t of the daily difference is ≥ 2 **and**
the sign matches discovery.

- **C1. Limit offset +2¢ vs the touch** (base rule): net ¢ per entry, entries where
  the cheaper limit fails count as 0. Discovery: +2.98 vs +2.44.
  If it holds, it is an execution change (the order's limit), not a decision change:
  it can ship to paper with no epoch bump of the decision.
- **C2. Threshold 0.15 vs 0.10**: net ¢ per window (per day), same per-entry
  accounting. Discovery: 890 vs 805 ¢/day at the touch limit. **Even if it holds,
  it is not promoted automatically**: 0.10 is a settled decision (2026-10-07) made
  for fill size, which this tape cannot measure beyond the touch. A holding C2 goes
  to the owner with the number, and to our tape's depth (3.2) before any change.

Everything else in the sweeps (t12/t20, cap 0.50, early start, Kalshi spread
filters, Polymarket spread, sizing) is reported on the holdout but cannot be promoted
from this holdout. They were not chosen in advance.

## Not tested here

- Size at the real fill: needs our tape's depth (`research.fills`), on Dublin hours
  after phase 1.
- Markouts (gate: median 5 s ≥ −3¢): needs the live `markout` records.
