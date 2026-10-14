# Pre-registration: Helix holdout (PLAN 0.3, 3.3)

> **Commands (2026-10-09).** The service is Rust now. This record keeps the
> Python module names it was written with: `research.<x>` is `pk <x>`, and
> `research.helix_<x>` / `research.helix.<x>` is `pk helix <x>`. The
> numbers below were computed by the Python modules; the Rust ports match
> them on the parity tapes (`crates/pk-research/tests`).

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

---

# Part 4: assets, cap, threshold and the stale-frame filter (registered 2026-10-07, before deploy)

**What has been read.** The export `pk-export-dublin-dev-20261007T101722Z` (all of
epochs 3 and 4, and epoch 5 up to 10:13 UTC, about 17.5 h): 93 settled paper trades and
346 shadow entries. The hypotheses below come from that read and from the Helix
discovery and holdout, so none of them can be tested on it. The count for this part
starts at the deploy of the commit that adds this text. **Live parameters are unchanged**
(epoch 5: gap 0.15, cap 0.60, 20/60 s, 150 ms, +2¢ limit); this deploy moves only the
statistics into the observer process, which decides nothing. The part 2 and 3 counts
continue.

What the read showed (none of it significant):

- Without BTC and SOL the run's settled paper P&L is +$195.5 (n 56) against +$49.5
  (n 93). Most of the difference is BTC's size (an average $41 filled, against $6–22 on
  the other assets), not its edge per share: BTC −3.0¢/share, BNB −13.9, DOGE −14.5, SOL
  −14.8, HYPE +8.8, XRP +19.8, ETH +26.0 (ETH's +$207 is mostly three 0.20-era trades).
  BTC was also the one asset below zero on Helix: −0.2¢ in the holdout (n 317), −0.9¢ on
  our tape after fills.
- The 0.10 era lost −$284.0 in all (−$70.1 without BTC and SOL); the 0.20 hour made
  +$295.9 and epoch 5 (0.15) +$37.6.
- Helix: cap 0.50 +8.44¢ vs base +6.91¢ (holdout); 0.20 beats 0.15 per day at 150 ms and
  +2¢ in discovery (1,262 vs 1,096 ¢/day); a Polymarket spread of 1¢ or less +9.1¢, above
  0.10 −2.3¢.
- At decisions the Polymarket feed's frame lag was p50 67 ms, p90 231 ms, and the leg's
  book age reached 1.9 s (p99).

Tests (each on ≥ 300 settled `live` entries counted from this deploy, the daily
difference's day-t ≥ 2, window-clustered s.e. reported; a holding result goes to the
owner with the numbers, nothing is applied automatically):

- **A1. Without BTC and SOL.** `research.readout --exclude-assets BTC,SOL` against the
  full read: settled paper P&L and net ¢/share at the real fill, per day, and the `live`
  shadow net at the touch. Paper positions share no risk budget, so the reduced read is
  exactly what not trading them would have produced; BTC and SOL keep trading in paper.
  Read alongside: $ filled per entry per asset (BTC carries most of the capacity).
- **A2. Cap 0.50.** `cap50` minus `live`, net ¢/share at the touch per day.
- **A3. Threshold 0.20 under a 0.50 cap.** `t20_cap50` minus `live`, **sum** of net
  ¢/share per day (it trades less by design). `t20` is reported.
- **A4. Polymarket spread ≤ 1¢.** `pspread01` minus `live`, sum per day.
- **A5. Fresh Polymarket frame.** `plag250`: no decision while the cheap leg's latest
  frame arrived more than 250 ms after Polymarket stamped it (the window is not
  consumed). Sum per day. The lag includes clock skew between the hosts; it is read on
  the same host as `live`, so the skew is common to both.

Execution option, not a test: `PK_SIZE_TO_DEPTH=true` orders at most the shares offered
at or below the limit at the decision. On paper it changes almost nothing (the venue
fills only what is offered); it is the order size a live run would send, and it is in
the config hash when on.

**The AWS move.** If the paper host moves to AWS eu-west-1 (infra/systemd/README.md,
"Moving poly-kalshi to AWS"), the results epoch is bumped at the switch, because fills
depend on latency. Each test then counts the two epochs separately and reports both; it
passes only if the pooled day-t is ≥ 2 and the sign matches in each.

---

# Part 6: epoch 14, live only (registered 2026-10-14, before deploy)

**What has been read.** The export `pk-export-aws-euw1-dev-20261014T124537Z` (epochs
10–13), an outside review of it, and the 2026-10-14 journal on the host: 65 live
settlements across epochs 10–13 (+$3.71); BNB −$18.80 over 13 and XRP −$25.81 over 8,
pooled across configurations. The hypothesis below comes from that read, so none of it
can test it. The count starts at epoch 14's deploy.

**Shadows ended.** Epoch 14 removes the shadow variants and the paper twin (owner's
decision), so the open shadow tests end unread: part 3 P3 (`kext`) and P5 (`usnight`),
part 4 A2 (`cap50`), A3 (`t20_cap50`), A4 (`pspread01`) and A5 (`plag250`). What they
had accumulated by epoch 13 stays in the journals and is not a result.

- **B1. Without BNB and XRP.** `pk readout --epoch 14 --exclude-assets BNB,XRP`
  against the full read: settled live P&L and net ¢/share over the shares filled, per
  day. Test on ≥ 300 settled live entries from this deploy; the daily difference's
  day-t ≥ 2 sends it to the owner, with the per-asset counts. Nothing changes
  automatically: BNB and XRP keep trading live at full size meanwhile.

Read alongside, not tested: the share of exits sold at the new floor, and the share of
entries whose limit the net-edge cap pulled below touch + offset.
