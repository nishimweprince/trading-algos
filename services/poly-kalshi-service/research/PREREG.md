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

---

# Part 2: the live Dublin holdout (registered 2026-10-07, before deploy)

The Helix holdout is spent. The next one is the paper run on Dublin from the
deploy of this commit on, **results epoch 4**, read with `research.readout` (shadow
table) and `research.fills` (depth) once both counts below are reached. Nothing
from epoch 4 has been read when this is written.

Settings deployed: gap 0.10, cap 0.60, 20/60 s, 150 ms paper latency,
`PK_LIMIT_OFFSET=0.02` (C1 held on Helix), shadow variants as in
`shadow.DEFAULT_VARIANTS`, including the two below.

- **P1. Polymarket spread filter.** Discovery (Helix, 39 d): a crossing with a
  Polymarket YES spread over 0.10 lost 2.3¢/share at the touch. Variants
  `pspread10` (skip while the spread is over 0.10, not consuming) and `pspread05`.
  Test: `pspread10` net ¢/share at the touch minus `live`'s, per day, on ≥ 300 settled
  `live` entries. Promote if the day-t of the daily difference is ≥ 2.
  `pspread05` is reported, not tested.
- **P2. Threshold revisit (0.15 vs 0.10).** C2 held on price-side fills. What
  Helix could not measure is size. Test: the `t15` shadow variant against `live`,
  **$ net per window at the real fill**. `research.fills run --gap 0.15` and
  `--gap 0.10` are both run on the same epoch-4 tape, at 150 ms and +2¢, with full
  depth. A day-t of the daily difference ≥ 2 sends the change to the owner with
  the numbers. It is never applied automatically.
- **Gates unchanged** (README §Gates). The markout gate (median 5 s on fills
  ≥ −3¢) is read on the same data.

---

# Part 3: after epoch 4's first hour (registered 2026-10-07, before deploy)

**What has been read.** The first ~75 minutes of epoch 4, exported at 08:27 UTC, have been read: 4 windows from 07:15 to 08:15 UTC; 5 settled fills, 0 won; the `live` shadow had 16 entries, 0 won, −31¢/share. All of epoch 3 has also been read. The hypotheses below come from those reads, so neither can test them. The count for this part starts at the deploy of the commit that adds this text. Epoch 4 continues, and its live parameters are unchanged (`config` `c4466dd83b`), so part 2 is unaffected.

What the reads showed:

- In all 16 entries Polymarket was right and Kalshi wrong; both venues resolved the same way.
- In 15 of the 16, Kalshi's mid was closer to 0.5 than Polymarket's. Kalshi was trailing a move, not leading one. On epoch 3's recorded quotes, `decide(kalshi_extreme=True)` keeps 26 of 110 entries, and 20 of the 26 won (77%), against 38% for the rest.
- The hour was 03:15–04:15 ET.
- The 5 s markout median was +4.9¢, and every position was then held to a losing settlement.

Variants (`shadow.DEFAULT_VARIANTS`) and telemetry (`engine.EXIT_S`, `EXIT_GAP`) deployed:

- **P3. Kalshi leads, not trails.** Variant `kext`: a crossing where Kalshi's mid is no further from 0.5 than Polymarket's is skipped and the window is consumed.
  - Test: `kext` net ¢/share at the touch minus `live`'s, per day, on ≥ 300 settled `live` entries counted from this deploy.
  - A day-t of the daily difference ≥ 2 sends it to the owner.
  - `kext_t15` is reported, not tested.
- **P4. Exit on convergence instead of holding.** The rule `exit_gap` sells at the held leg's best bid the first time |Kalshi mid − Polymarket YES mid| ≤ 0.03 within 120 s of the order, and otherwise holds. It applies to every live order (fills and no-fills), with the price paid as `ref_px`, taker fee on both legs (`research.readout` → `exits.all`).
  - Test: `exit_gap` minus `hold`, per day, on the same orders and the same count.
  - A day-t ≥ 2 sends it to the owner. Exits mean live sell orders, so this is never automatic.
  - The fixed horizons `exit_5s` … `exit_120s` are reported, not tested.
  - Caveat: the bid size at the exit is recorded. If the median $ at the bid is below the median $ filled, the result is reported as capacity-bound.
- **P5. No trading in the US night.** Variant `usnight` takes no decision in UTC hours 5–10.
  - Test: `usnight` minus `live`, **sum** of net ¢/share at the touch per day, because it trades less by design. Same count; day-t ≥ 2 to the owner.
  - The shadow table's `us_night`/`us_day` split is reported.
- `floor15` (skip below 0.15) is reported, not tested.

Every comparison uses the window-clustered s.e. shown next to the per-entry s.e. Entries in one window move together: epoch 4's 16 came from 4 windows. The day-t tests above already treat a day as the unit.

**Discovery on Helix.** `research.helix_sweeps --split discovery` → `regime`. It runs the base rate of a bad hour, P(0 wins in an hour with ≥ 10 entries); net by UTC hour; the Kalshi-extremeness split; and clustering. The results go into RESULTS.md before any part-3 data is read. They can retire a hypothesis, which is then reported and not tested. They cannot add one to this part.

---

**2026-10-07, epoch 5: the live threshold is 0.15** (owner's decision after C2). This
ends the epoch-4 holdout for parts 2 and 3: their counts are read on epoch 4 as far
as it went, and nothing from epoch 4 decided this change. From epoch 5, `live` is
0.15 and the `t10` shadow variant measures the old rule; the part 2 and 3 tests
run unchanged against `live`, counted from epoch 5's start.
