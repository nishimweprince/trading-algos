# `session-hedging`: Improvement and Implementation Specification, v3

> **Supersedes v2.** This revision incorporates the user's own three-timeframe backtest exports
> (M15, H1, H4 on XAUUSD, `performance_unit=pips`). Those runs settle several questions v2 left
> open, invalidate one whole timeframe on structural grounds, and force a change in how timeframe
> is treated in the codebase. Changes are marked **[v3]**.
>
> **Audience.** Claude Code, working in the `session-hedging` repository.
>
> **Prime directive, unchanged.** Do not tune parameters before fixing measurement.

---

## 0. What the three timeframe runs establish

### 0.1 Headline results as exported

| | M15 | H1 | H4 |
|---|---:|---:|---:|
| Closed pairs | 66 | 259 | 981 |
| Sample window | 21 Jul to 19 Aug 2026 | 20 Apr to 19 Aug 2026 | 6 May 2025 to 18 Aug 2026 |
| Span | **29 days** | **120 days** | **469 days** |
| Total | **−1,644 pips** | **+4,876 pips** | **+141,571 pips** |
| Expectancy | −24.9 pips | +18.8 pips | +144.3 pips |
| Expectancy in R | **−0.0745R** | **+0.0811R** | **+0.2097R** |
| Profit factor | 0.795 | 1.071 | 1.304 |
| Win rate | 28.8% | 35.5% | 39.8% |
| Median 1R (`S`) | 163 pips | 351 pips | 640 pips |
| Median hold | 5.2h | 25.0h | 124.0h |
| p95 hold | 60h | 167h | 784h |
| Max concurrent pairs | 4 | 10 | **43** |
| Max drawdown | 13.3R | 24.8R | 88.7R |
| Break-even cost (4 sides) | none, already negative | **4.7 pips/side** | 36.1 pips/side |

Read naively this says "use H4". That reading is wrong for three independent reasons, each
verifiable from the exports themselves.

### 0.2 Finding 1: the H4 session anchor is broken [v3]

Session membership is judged on the **bar's open** (`strategy.md` §3). No H4 bar opens at a session
open, so the first member bar can be up to one full bar late, and the one-bar entry delay adds
another. Reconstructing the signal and entry bar opens from the exports:

| Timeframe | Session | Signal bar opens | Entry bar opens | Nominal session open | Drift |
|---|---|---|---|---|---|
| M15 | tokyo | 00:00 UTC | 00:15 UTC | 00:00 UTC | **+15 min** |
| M15 | london | 07:00 UTC | 07:15 UTC | 07:00 UTC | **+15 min** |
| M15 | new_york | 12:00 UTC | 12:15 UTC | 12:00 UTC | **+15 min** |
| H1 | tokyo | 00:00 UTC | 01:00 UTC | 00:00 UTC | **+60 min** |
| H1 | london | 07:00 UTC | 08:00 UTC | 07:00 UTC | **+60 min** |
| H1 | new_york | 12:00 UTC | 13:00 UTC | 12:00 UTC | **+60 min** |
| H4 | tokyo | 01:00 UTC | **05:00 UTC** | 00:00 UTC | **+5 hours** |
| H4 | london | 09:00 UTC | **13:00 UTC** | 07:00 UTC | **+6 hours** |
| H4 | new_york | 13:00 UTC | **17:00 UTC** | 12:00 UTC | **+5 hours** |

On H4 the labels are fiction. The "london" trades are entered at 13:00 UTC, which is the **New York
open**. The "new_york" trades are entered at 17:00 UTC, which is **mid-afternoon New York**. The
"tokyo" trades are entered at 05:00 UTC, which is **Tokyo afternoon, just before London**.

H4 is therefore not a test of this strategy. Whatever it measures, it is not session-open
expansion. Its numbers cannot be compared to M15 and H1 because it is not running the same
strategy.

M15 and H1 both anchor correctly. H1 uses the first hour as its opening range and enters 60
minutes after the open, which is a coherent design (an initial-balance style breakout). M15 uses
the first 15 minutes and enters 15 minutes after.

### 0.3 Finding 2: the timeframe comparison is confounded by sample period [v3]

The three runs cover different periods, so the headline ranking is mostly a ranking of market
regimes.

**Common window, 21 Jul to 19 Aug 2026, all three timeframes:**

| | M15 | H1 | H4 |
|---|---:|---:|---:|
| Pairs | 66 | 64 | 52 |
| Total pips | −1,644 | −721 | +802 |
| Expectancy in R | **−0.0745R** | **−0.0427R** | **−0.0528R** |
| Profit factor | 0.795 | 0.954 | 1.036 |

**All three are negative in R in the only window where all three ran.**

**H1 versus H4 on their shared window, 20 Apr to 19 Aug 2026:**

| | H1 | H4 |
|---|---:|---:|
| Pairs | 259 | 247 |
| Expectancy in R | +0.0811R | **+0.0701R** |
| Profit factor | 1.071 | 1.085 |

Essentially tied. H4's headline `+0.2097R` comes almost entirely from the period before April 2026,
which is not in the H1 sample at all.

There is also a visible **trend confound** in H4. Among pairs that reached the survivor target,
the winning leg was long in **64%** of H4 cases against 39% in H1 and 58% in M15. Over the H4
window gold rose from roughly 3,377 to above 4,800. A symmetric straddle in a strongly trending
instrument collects the drift, which is a real effect but a regime-dependent one, not evidence
that H4 bars are better.

### 0.4 Finding 3: timeframe is a proxy for holding horizon [v3]

The exports show that going up a timeframe changes three things at once, and the apparent
improvement tracks the third.

| | M15 | H1 | H4 |
|---|---:|---:|---:|
| Opening range window | 15 min | 60 min | 240 min |
| Median `S` (1R) | 163 pips | 351 pips | 640 pips |
| Entry lag after the open | 30 min | 120 min | 480 min |
| Median hold | 5.2h | 25.0h | 124.0h |
| **Survivor-TP rate** | **28.8%** | **34.7%** | **39.6%** |

The `3R` target needs the survivor to travel `6 x` the opening range. The independent pilot found
such moves are reached by only 0 to 7% of sessions within an hour, but 53 to 62% within 24 hours.
So a higher timeframe raises the target-hit rate **mainly by holding longer**, not by producing a
better signal. That is a real effect, but it is bought with holding time, swap, and concurrent
exposure rather than with edge.

Where the money actually sits confirms this:

| Holding bucket | M15 share of total R | H1 | H4 |
|---|---:|---:|---:|
| under 12h | (loss-making) | +3% | −1% |
| 12 to 24h | −129% (small n) | **+75%** | −13% |
| 24 to 72h | +154% (loss) | −9% | −15% |
| 72 to 168h | +38% (loss) | +35% | +34% |
| over 168h | 0 | −4% | **+95%** |

**H1 earns three quarters of its result inside 24 hours.** A 24-hour maximum age costs H1 almost
nothing (pairs held beyond 24h contribute 3% of its total R). The same cap would remove essentially
all of H4's result, since 95% of it comes from pairs held longer than a week. That is decisive for
a drawdown-limited prop account.

### 0.5 Finding 4: the arithmetic that governs everything [v3]

Every surviving-target win is exactly `+2.00R`. Every lock exit is `−1R + lock`, which the exports
put at a mean of `−0.875R` (M15), `−0.942R` (H1), `−0.963R` (H4). So the strategy's viability
reduces to a single inequality:

> **survivor-TP rate must exceed `|mean loss| / (2 + |mean loss|)`**

| | Required TP rate | Actual TP rate | Margin |
|---|---:|---:|---:|
| M15 | 30.4% | 28.8% | **−1.6 pp** |
| H1 | 32.0% | 34.7% | **+2.7 pp** |
| H4 | 32.5% | 39.6% | +7.1 pp (invalid anchor) |

Margins of a few percentage points on samples of 66 and 259 are not statistically distinguishable
from zero. **This single statistic should become the headline metric of the project**, reported on
every run with a confidence interval, because it is the whole strategy in one number.

The whipsaw everyone worries about is not the problem: `−2R` double-stops occurred in only 3.0%,
3.1% and 1.2% of pairs. The modal outcome is the lock exit (68%, 59%, 46%), which is the strategy
paying `−0.9R` to be told it was wrong.

### 0.6 Finding 5: the cost budget is thin, and negative on M15 [v3]

Expressed the way this project reports, in pips per transaction side, with four sides per pair:

| Cost per side | M15 expectancy | H1 expectancy | H4 expectancy |
|---:|---:|---:|---:|
| 0 pips | −0.1523R | +0.0536R | +0.2255R |
| 1 pip | −0.1768R | +0.0422R | +0.2192R |
| 2 pips | −0.2012R | +0.0308R | +0.2130R |
| 3 pips | −0.2257R | +0.0194R | +0.2067R |
| **break-even** | **already negative** | **≈4.7 pips/side** | ≈36 pips/side |

A realistic gold spread plus commission plus stop slippage lands around 2 to 4 pips per side. H1
retains a fraction of its edge at that level; M15 has no budget at all; H4's apparent headroom is a
regime artifact on a broken anchor.

### 0.7 Finding 6: the unit bug is real and it flips signs [v3]

v2 §1 predicted that averaging raw pips across variable `S` gives a different answer from averaging
R. The exports confirm it, with **sign disagreements**:

| | mean pips per pair | mean R per pair |
|---|---:|---:|
| M15 tokyo | **−39.3 pips** | **+0.0148R** |
| M15 london | **−12.5 pips** | **+0.0545R** |
| H1 tokyo | **−5.6 pips** | **+0.0377R** |
| H4 common window | **+15.4 pips** | **−0.0528R** |

Four cases where the pip total and the R total disagree about whether the strategy made money.
Under fixed lots the R figure is the one that describes an account; under fixed-fractional sizing
`pips_weighted` becomes the correct additive series. The §1 unit policy is now mandatory, not
advisory.

### 0.8 Finding 7: concurrency scales badly with timeframe [v3]

| | Max concurrent pairs | Median | p90 |
|---|---:|---:|---:|
| M15 | 4 | 1 | 3 |
| H1 | 10 | 5 | 7 |
| H4 | **43** | **19** | 28 |

Nineteen simultaneous open hedged pairs, each with two legs, is 38 open positions as a median
state. No prop evaluation survives that, and margin alone would likely prevent it. The
`MAX_CONCURRENT_STRUCTURES` and `ONE_OPEN_PER_SESSION` caps in v2 §3.5 are now empirically
justified rather than precautionary.

---

## 1. Timeframe recommendation [v3, new section]

### 1.1 The recommendation

**Orient to the H1 scale, but do not run the engine on H1 bars.**

Run the engine at **M15 resolution or finer** and express the H1 strategy through parameters:

| Axis | Setting | Reason |
|---|---|---|
| Bar resolution (fill and level checking) | **M15**, with M1 for path resolution | Level crossings must be checked as finely as possible. On H4 a stop and a target four hours apart are adjudicated by one OHLC bar |
| Opening range window | **60 minutes** (`ORB_MINUTES=60`) | This is what actually made H1 work. It is a volatility estimate, not a bar size |
| Entry lag after the anchor | **time-based**, start at 15 to 60 min | Currently one bar, which is why H4 drifts 5 hours |
| Maximum holding age | **24 to 48h** (`MAX_AGE_HOURS`) | H1 earns 75% of its R inside 24h and only 3% beyond it |

### 1.2 Why not simply run H1 bars

Three reasons, in order of importance.

1. **Path resolution.** The measurement risk this project has is intrabar ambiguity. A larger bar
   is strictly worse: it puts more price action inside a single OHLC candle that the engine must
   adjudicate with an assumption. The independent pilot showed a 69R swing on M15 alone between
   optimistic and pessimistic assumptions. On H4 that swing would be far larger. Bar size for
   fills should go **down**, not up.
2. **Anchor precision.** The one-bar entry delay is a bug in disguise: it makes the entry lag a
   function of the bar size rather than a design choice. Decoupling it fixes H4-style drift
   permanently and makes the lag testable.
3. **They are not actually different strategies.** "H1" is "60-minute opening range, ~1 day hold".
   Once `ORB_MINUTES` and `MAX_AGE_HOURS` exist, the timeframe axis collapses into parameters that
   can be swept, which is what you want.

### 1.3 What to do with H4

Retire it as a configuration. Its anchor is broken, its result is regime-dependent and trend-loaded,
its concurrency is prop-fatal, and 95% of its P&L requires week-long holds.

If the multi-day behaviour is independently interesting, that is a **different strategy** (a slow
trend-following hedge on gold) and deserves its own spec, its own anchors, and its own risk model.
Do not let it ride along inside this one, where it will contaminate every aggregate statistic.

### 1.4 What to do with M15

Keep it as the **resolution** layer and as a scale variant to test, but note it failed on its own
terms: `−0.0745R` over 66 pairs with a TP rate 1.6 pp below the break-even requirement and no cost
budget. The most likely explanation, consistent with everything above, is that a 15-minute opening
range gives a stop too tight relative to the `6 x range` the target demands, so the survivor gets
locked out before the move develops. That hypothesis is directly testable by sweeping
`ORB_MINUTES` at fixed resolution, which is exactly what §1.1 enables.

### 1.5 Baseline configuration to build toward

```
INTRABAR_MODE=m1_conservative
BAR_TIMEFRAME=M15
ORB_MINUTES=60                 # the H1 finding, expressed as a parameter
ENTRY_DELAY_MINUTES=15         # decoupled from bar size, sweep 0/15/30/60
ANCHOR_TOLERANCE_MINUTES=15    # reject signals that drifted off the anchor
STOP_MODE=bar_range            # fixed_pips is available as a control, see §4.3
SL_MULT=2.0                    # sweep, but 2.0 has support
RR=3.0                         # under test, see §9
LOCK_MODE=breakeven
MAX_AGE_HOURS=24               # sweep 12/24/48
TIME_EXIT_MODE=max_age
RISK_MODE=fixed_fractional
RISK_PCT_PER_R=0.10
MAX_CONCURRENT_STRUCTURES=3
ONE_OPEN_PER_SESSION=true
COST_MODEL=per_session
ENTRY_MODE=hedge_pair          # with synthetic_breakout as the mandatory control
```

---

## 2. Unit policy: performance is counted in pips

Unchanged from v2 §1, now with empirical justification from §0.7.

| Unit | Definition | Use |
|---|---|---|
| `pips_raw` | `(exit - entry) / pip_size`, signed, unscaled | Legacy continuity, per-leg diagnostics, MAE/MFE geometry |
| **`pips_weighted`** | `pips_raw x (qty / QTY_REF)` | **Primary additive performance series.** Identical to `pips_raw` under fixed lots |
| `r_multiple` | `pips_raw / (S / pip_size)` | Cross-trade comparability. **The sign-of-truth when `S` varies** |
| `cash` | via `DOLLARS_PER_PIP_PER_QTY x QTY_REF` | PropGuard only |

- Costs denominated in pips: `spread_pips`, `slippage_pips`, `commission_pips`, `swap_pips`.
- Equity curve and drawdown headline on `pips_weighted`, with `max_drawdown_r` alongside because
  it is the only figure comparable across timeframes and stop widths.
- **Every report that shows a pip total must show the R total beside it.** §0.7 shows they can
  disagree in sign, and a report showing only one of them is capable of being actively misleading.
- Remove or rename the mixed-unit `equity` (`strategy.md` §13.7). `POINT_VALUE` configurable, from
  broker contract spec, never inferred from `pip_size`.

**Acceptance.** `test_pips_weighted_equals_pips_raw_under_fixed_lot`,
`test_pips_weighted_is_additive_under_variable_sizing`, `test_report_shows_pips_and_r_together`,
`test_cost_pips_and_cash_agree`.

---

## 3. Target architecture

```
src/
├── engine.py
├── entry/  base.py | hedge_pair.py | synthetic.py | contingent.py | oco_bracket.py
├── anchors.py       # [v3] anchor definitions, drift detection, tolerance enforcement
├── units.py         # pips_raw / pips_weighted / R / cash
├── sizing.py, costs.py, fills.py, filters.py, exits.py, indicators.py
├── validation.py, metrics.py, firm_profile.py, risk_guards.py
├── research/  mfe_study.py | break_study.py | anchor_study.py | scale_study.py
│              walkforward.py | overfitting.py | montecarlo.py
└── models.py, sessions.py, candles.py, paper.py, notifier.py, api.py, config.py, main.py
```

`scale_study.py` is **[v3]** and replaces the idea of running separate timeframes: it sweeps
`ORB_MINUTES x ENTRY_DELAY_MINUTES x MAX_AGE_HOURS` at fixed resolution.

---

## 4. Configuration surface

Only deltas from v2 are shown in full; unchanged keys are listed for completeness.

### 4.1 Scale and anchoring [v3, substantially revised]

| Key | Type | Default | Notes |
|---|---|---|---|
| `BAR_TIMEFRAME` | str | `M15` | **Resolution only.** No longer carries strategy meaning |
| `ORB_MINUTES` **(new)** | int | **`60`** | **[v3]** Opening-range window, independent of bar size. Must be a multiple of the bar. Sweep 15/30/60/120 |
| `ENTRY_DELAY_MINUTES` **(new)** | int | **`15`** | **[v3] Replaces the one-bar delay.** Sweep 0/15/30/60 |
| `ANCHOR_TOLERANCE_MINUTES` **(new)** | int | **`15`** | **[v3]** If the signal bar's open is more than this far after the anchor, **skip the signal and emit an event**. This is the permanent fix for H4-style drift |
| `SESSION_ANCHORS` **(new)** | list `name:TZ:HH:MM` | current three | Anchors are explicit times, not window edges |
| `PER_SESSION_PARAMS` **(new)** | bool | `true` | Independent `SL_MULT`, `RR`, `LOCK_*`, `MAX_AGE_HOURS` per session |
| `HOLIDAY_CALENDAR` **(new)** | path | `""` | Currently absent (`strategy.md` §3) |

**`ANCHOR_TOLERANCE_MINUTES` is the single most valuable new key in v3.** Had it existed, the H4
run would have produced zero signals instead of 981 mislabelled ones. It converts a silent
correctness failure into a loud one.

Anchor grid to test (unchanged from v2 §3.2): Tokyo 09:00 and **08:45** JST; London 08:00, 10:30,
15:00; New York 08:00, **08:20**, 08:30, 09:30 ET; Sydney only via a broker liquidity restart.

### 4.2 Entry structure

| Key | Default | Notes |
|---|---|---|
| `ENTRY_MODE` | `hedge_pair` | `hedge_pair` \| `synthetic_breakout` \| `contingent_hedge` \| `oco_bracket` |
| `SYNTH_TRIGGER` | `s_distance` | Payoff-matched control at `entry ± S` |
| `HEDGE_RATIO_INITIAL` / `HEDGE_TRIGGER_MODE` / `HEDGE_FAILURE_K` / `HEDGE_RATIO_STAGED` | `0.0` / `failure_zone` / `0.5` / `1.0` | Contingent hedge |
| `OCO_BUFFER_MODE` / `OCO_BUFFER_VALUE` / `OCO_EXPIRY_BARS` | `atr_frac` / `0.10` / `4` | Bracket variant |
| `ALLOW_REENTRY` | `false` | Max one per session, tagged |
| `SKIP_DOJI` | `true` | Unchanged |

### 4.3 Stop sizing

| Key | Default | Notes |
|---|---|---|
| `STOP_MODE` | `bar_range` | **Implemented:** `bar_range` \| `fixed_pips`. `bar_range` means "range over `ORB_MINUTES`", not "range of one bar" |
| `SL_MULT` | `2.0` | `bar_range` only. Has support: the independent pilot found 1.0 and 1.5 clearly negative |
| `FIXED_STOP_PIPS` **(new)** | `0` | **Implemented.** `fixed_pips` only, and required in that mode: `S = FIXED_STOP_PIPS x PIP_SIZE`, independent of the opening range |
| `ATR_PERIOD` / `ATR_MULT` / `BLEND_WEIGHT` | `14` / `1.25` / `0.5` | Not implemented |
| `MIN_STOP_PIPS` | derived | **Must exceed round-trip cost.** Compute from `costs.py` per session, emit the derived floor. Applies as a floor in **both** modes |
| `MAX_STOP_PCTL` / `MAX_STOP_ACTION` | `0.90` / `skip` | Rolling percentile, not a fixed pip cap. Not implemented |

**`STOP_MODE=fixed_pips` is a measurement control, not a tuning shortcut.** Under `bar_range`, `S`
varies per session, so `R` is a different quantity in every pair and the pip and R series can
disagree in sign (§0.7). Pinning `S` makes `R` a constant, which separates "the edge changed" from
"the denominator changed". The default stays `bar_range` so every cell measured in Phase 0 remains
comparable; a `fixed_pips` run is a **different cell** and must be labelled as one. The report
header and `/v1/config` both state `stop_mode` and `fixed_stop_pips` for that reason.

Note that `fixed_pips` decouples the stop from volatility entirely, so a fixed `S` that is
comfortable in Tokyo may be inside the noise at the New York open. Do not read a `fixed_pips`
result as evidence about `SL_MULT`.

### 4.4 Targets, locks, exits

| Key | Default | Notes |
|---|---|---|
| `RR` | `3.0` | **Under test.** See §9. Do not tune off a single sweep |
| `TP_MODE` / `PARTIAL_TP_R` / `PARTIAL_FRACTION` | `fixed_r` / `1.0` / `0.5` | |
| `TRAIL_MODE` / `TRAIL_ATR_MULT` / `TRAIL_ACTIVATE_R` | `none` / `2.0` / `1.0` | |
| `LOCK_MODE` | **`breakeven`** | Pilot: BE beat the `$2` lock; `$5` was negative. The exports show lock exits are the **modal outcome** (68%/59%/46% of pairs), so this parameter dominates expectancy |
| `LOCK_TRIGGER_R` / `LOCK_LEVEL_R` | `1.0` / `0.1` | For `r_relative` |
| `MAX_AGE_HOURS` | **`24`** | **[v3] Now evidence-backed on the user's own data**: H1 earns 75% of R inside 24h and 3% beyond |
| `TIME_EXIT_MODE` | `max_age` | |
| `FLAT_BEFORE_WEEKEND` | `true` | p95 holds of 167h (H1) cross weekends routinely |

### 4.5 Risk and sizing

| Key | Default | Notes |
|---|---|---|
| `RISK_MODE` | `fixed_qty` | Preserved for parity testing |
| `RISK_PCT_PER_R` | `0.10` | Drawdowns of 13.3R (M15) and 24.8R (H1) imply 1.3% and 2.5% at this sizing |
| `MAX_PAIR_RISK_PCT` | `0.20` | Whipsaw reserve. Whipsaw frequency measured at 1.2 to 3.1% |
| `MAX_OPEN_RISK_PCT` | `0.75` | |
| `MAX_CONCURRENT_STRUCTURES` | **`3`** | Measured maxima: 4 / 10 / 43 |
| `ONE_OPEN_PER_SESSION` | **`true`** | The mechanism behind the 43-way stack |
| `QTY_REF` / `CONTRACT_SIZE` / `POINT_VALUE` | current `QTY` / `100` / configurable | |

### 4.6 Costs and measurement

Unchanged from v2 §3.6 and §3.7. `INTRABAR_MODE` defaults to `m1_conservative`.
`BREAKEVEN_COST_REPORT=true`.

---

## 5. Phase 0: Measurement correctness (blocking)

### W0.0 [v3, NEW]: Anchor drift detection

**Problem.** `strategy.md` §3 judges session membership on the bar's open. At coarse resolution the
first member bar can be an arbitrary distance after the anchor. Measured drift: 15 min on M15, 60
min on H1, **5 to 6 hours on H4**, where the "London" signal is entered at the New York open.

**Change.** `src/anchors.py`:
- Anchors are explicit times, not window edges.
- Compute `anchor_drift_minutes = signal_bar_open - anchor_time` for every signal.
- If drift exceeds `ANCHOR_TOLERANCE_MINUTES`, **skip and emit `signal_skipped_anchor_drift`**.
- Report `anchor_drift` distribution per session, and the skip count.
- The opening range is measured over `ORB_MINUTES` **from the anchor**, not over one bar.
- Entry occurs `ENTRY_DELAY_MINUTES` after the opening range closes, not one bar later.

**Acceptance.**
- `test_h4_style_drift_is_rejected`: an H4-resolution run against the current anchors produces
  zero signals rather than mislabelled ones.
- `test_orb_window_independent_of_bar_size`: `ORB_MINUTES=60` yields an identical opening range on
  M15 and M1 inputs.
- `test_entry_delay_is_time_based_not_bar_based`.
- Report exposes `anchor_drift_p50` and `anchor_drift_max` per session.

**Do this first.** It is cheap, it is the only defect that silently invalidates an entire run, and
every study below depends on signals being where they claim to be.

### W0.1: Intrabar path resolver ladder

Unchanged from v2 §4 W0.1. Tiers: `optimistic`, `pessimistic`, `m1`, **`m1_conservative`
(default)**, `tick` (interface only).

**[v3] Added context.** The exports show 10.6% (M15), 11.2% (H1) and 5.1% (H4) of pairs resolve
both legs within a single bar, and those pairs carry a disproportionate share of the losses (on
M15 they account for 75% of the total loss). The Branch B bias in this data therefore looks
**smaller** than the independent pilot's 69R spread implied, since the engine is already booking
most same-bar cases as lock exits rather than as targets. That moderates the concern; it does not
remove it, because the correct resolution of those 5 to 11% of pairs is still unknown without M1.

**[v3] Added acceptance criterion.** Report `same_bar_resolution_rate` and the R contribution of
same-bar pairs, per timeframe and per session, so this stays visible.

### W0.2 to W0.6

Unchanged from v2: warmup and spurious first signal; unlocked single-leg stop skip; candle
validation; metric set; drawdown on `pips_weighted` with M1 sampling and persistence.

**[v3] Addition to W0.5.** The metric set gains, as **headline** fields:

| Field | Why |
|---|---|
| `survivor_tp_rate` | The whole strategy in one number |
| `mean_loss_r` | Mean R of non-TP resolved pairs |
| **`breakeven_tp_rate_required`** = `abs(mean_loss_r) / (2 + abs(mean_loss_r))` | The bar to clear |
| **`tp_rate_margin_pp`** and its confidence interval | Whether the margin is distinguishable from zero |
| `outcome_mix` | TP / lock / breakeven / whipsaw shares |
| `anchor_drift_p50`, `anchor_drift_max` | From W0.0 |
| `same_bar_resolution_rate` | From W0.1 |
| `max_concurrent_structures`, `median_concurrent` | Measured 4 / 10 / 43 |

Measured reference values for regression testing: TP rates 28.8% / 34.7% / 39.6%, required rates
30.4% / 32.0% / 32.5%, whipsaw 3.0% / 3.1% / 1.2%, lock-exit share 68% / 59% / 46%.

---

## 6. Phase 1: Costs and risk

Unchanged from v2 §5 (cost model in pips, fixed-fractional sizing with slippage allowance in the
denominator, firm profile and PropGuard on equity including floating P&L).

**[v3] Added acceptance criterion for W1.1.** The break-even cost report must reproduce the
measured figures on the supplied exports: no positive budget on M15, approximately **4.7 pips per
side** on H1 across four transaction sides, and approximately **9.4 pips per side** for a two-side
control. If the implementation disagrees with these, the cost accounting is wrong.

**[v3] Added acceptance criterion for W1.2.** With `ONE_OPEN_PER_SESSION=true` and
`MAX_CONCURRENT_STRUCTURES=3`, a re-run of the H1 export period must show max concurrency of 3 or
fewer against the measured 10, and the report must state how many signals were suppressed by the
cap so the opportunity cost is visible.

---

## 7. Phase 2: `ENTRY_MODE` and the mandatory control

Unchanged from v2 §6: W2.1 parity gate, W2.2 `synthetic_breakout` (payoff-matched control at
`entry ± S`), W2.3 `contingent_hedge`, W2.4 `oco_bracket`, W2.5 comparison harness.

**[v3] Sharpened rationale for W2.2.** The exports make the control's value concrete. Every
survivor-target win is exactly `+2.00R` in all three timeframes, which is the mechanical signature
of the payoff identity in v2 §0.3: after the first stop, the pair *is* a single breakout position.
The hedge contributes nothing to that `+2.00R`; it only pays for it. On H1, moving from four
transaction sides to two roughly doubles the cost budget, from 4.7 to 9.4 pips per side, which on a
strategy this thin is the difference between tradeable and not.

---

## 8. Phase 3: Strategy redesign

Unchanged from v2 §7, with one addition.

### 8.1 [v3] The scale sweep replaces the timeframe question

Instead of running separate M15, H1 and H4 backtests, sweep at fixed M15 resolution:

`ORB_MINUTES ∈ {15, 30, 60, 120}` x `ENTRY_DELAY_MINUTES ∈ {0, 15, 30, 60}` x
`MAX_AGE_HOURS ∈ {8, 12, 24, 48}`

This reproduces the H1 configuration as one cell (`60 / 60 / ~24`) and the M15 configuration as
another (`15 / 15 / none`), on the **same data, same period, same resolution, same costs**, which
is the comparison the three exports could not provide. Run it on the longest available history, not
on three different windows.

**Hypothesis to test explicitly**: M15's failure is caused by too tight a stop relative to the
`6 x range` the target demands, not by the 15-minute anchor. If true, `ORB_MINUTES=60` with M15
resolution should recover most of H1's result while keeping M15's finer path resolution and shorter
entry lag. If false, the opening-range width is not the operative variable and the holding horizon
is.

---

## 9. Decision gates [v3 updated]

| Question | Evidence | Gate |
|---|---|---|
| **Are signals where they claim to be?** | W0.0 `anchor_drift` distribution | Any configuration with p50 drift beyond `ANCHOR_TOLERANCE_MINUTES` is **void**, not underperforming. H4 is the worked example |
| **Does the TP rate clear its bar?** | `tp_rate_margin_pp` with a confidence interval | Require the lower bound of the interval above zero. Measured margins of −1.6pp (n=66) and +2.7pp (n=259) are **not** distinguishable from zero |
| What scale? | §8.1 sweep on one window | Choose the cell, not the timeframe. Report the full surface, and prefer a broad plateau over a peak |
| Does the hedge earn two extra sides? | `hedge_pair` vs `synthetic_breakout`, net pips | The control roughly doubles the cost budget. The hedge must repay that in expectancy or breach probability |
| Enough cost headroom? | Break-even pips/side vs measured broker spread | Require **2x** headroom. H1's 4.7 pips/side against a realistic 2 to 4 is roughly 1.2 to 2.4x. Marginal |
| What `RR`? | S1 conditional hit rates crossed with `MAX_AGE_HOURS` | Not from a single sweep |
| Does the lock help? | `LOCK_MODE` sweep under walk-forward | Lock exits are the modal outcome (46 to 68%), so this parameter dominates. Expect `breakeven` or `none` |
| Holding horizon? | Holding-bucket R attribution | H1's 24h prior is supported on the user's own data. Confirm out of sample and price the swap |
| Is the edge real? | Walk-forward, deflated Sharpe, PBO | Positive in most folds, no single session dominating, broad plateau |
| Prop-survivable? | Monte Carlo under the firm profile | Concurrency capped, breach probability well under the limits |

---

## 10. Phase 4: Research harness

Unchanged from v2 §8 (S1 conditional target-hit, S2 single vs double break, S3 anchor study,
S4 cost sensitivity, S5 resolver ladder bias, S6 nested walk-forward, S7 prop Monte Carlo), plus:

### S8 [v3, new]: Scale decomposition

Run the §8.1 sweep and report, for each cell: `survivor_tp_rate`, `breakeven_tp_rate_required`,
margin with CI, expectancy in pips and R, break-even cost per side, median and p95 hold, max
concurrency, and the R attribution by holding bucket. Output the surface, not just the argmax.

**Purpose**: to answer "which timeframe" as a parameter question on one dataset, rather than as
three incomparable backtests on three different periods.

### S9 [v3, new]: Regime and trend attribution

Split every result by gold trend regime (for example a rolling daily slope, or simply calendar
halves) and report the long-versus-short split of surviving winners. The H4 export showed 64% of
winners long against 39% on H1, over a period when gold rose sharply. Any configuration whose edge
comes predominantly from one directional regime must be flagged, because a symmetric straddle
collecting trend drift is a regime bet wearing a hedge costume.

---

## 11. Phase 5, testing, reporting

Unchanged from v2 §10, §11, §12, with these additions:

- **[v3]** Reports lead with pips **and** R side by side (§0.7 shows they can disagree in sign).
- **[v3]** Report header states `BAR_TIMEFRAME`, `ORB_MINUTES`, `ENTRY_DELAY_MINUTES`,
  `ANCHOR_TOLERANCE_MINUTES`, `STOP_MODE` (with `FIXED_STOP_PIPS` when it applies), and the
  measured `anchor_drift_p50` per session.
- **[v3]** The four-mode comparison view gains the `survivor_tp_rate` versus
  `breakeven_tp_rate_required` panel, since that is the fastest read on whether a configuration is
  alive.
- **[v3]** Add a regression fixture built from the supplied M15 and H1 exports so the metric
  implementations can be validated against known figures before being trusted on new runs.

---

## 12. What this spec is not confident about

- **The M15 sample is 29 days and 66 pairs.** Its `−0.0745R` is not a verdict on M15, it is a
  month. Do not conclude M15 is dead; conclude it is untested.
- **The H1 sample is 120 days and 259 pairs** with a `+2.7pp` TP-rate margin. That is a plausible
  edge, not a demonstrated one. It needs walk-forward before it means anything.
- **H4's numbers are not usable**, but its underlying multi-day behaviour might still be a real
  phenomenon. Retiring it here is a scoping decision, not a claim it holds no information.
- **The trend confound is unquantified.** The 64/36 winner split is suggestive; S9 exists to
  measure it properly.
- **All three exports appear to come from the current engine**, so they inherit its optimistic
  same-bar handling, its lack of a cost model, and its fixed lot sizing. Every figure quoted in
  §0 is a **gross, pre-cost, optimistic-path** number, and the corrected versions will be worse.
- **Costs are still modelled, not measured.** Broker bid/ask ticks must replace the assumptions
  before any cost conclusion is final.
- **The strategy may still not survive.** An H1 TP-rate margin of 2.7 points on 259 samples, a
  cost budget of 4.7 pips per side against a realistic 2 to 4, and a modal outcome that loses 0.94R
  is a thin thing. §9 exists so a negative result is recognised as a result.

---

## 13. Execution order for Claude Code

1. **W0.0 anchor drift** (new, cheap, invalidates whole runs if wrong)
2. W0.2 warmup, W0.4 validation, W0.3 single-leg stop skip
3. W5.7 M1 seeding
4. W0.1 resolver ladder, then S5, checked against the same-bar rates in §0
5. §2 unit policy (`units.py`), then W0.5 metrics (including the TP-rate margin panel), W0.6
6. W1.1 costs (validate against the 4.7 pips/side figure), W1.2 sizing, W1.3 firm profile
7. W2.1 parity gate, then W2.2 synthetic control, W2.3, W2.4, W2.5
8. **S8 scale sweep** (this is the real answer to the timeframe question)
9. S1, S2, S3, S4, S9
10. Phase 3, driven by S8 and the §9 gates
11. S6 walk-forward, S7 Monte Carlo
12. Phase 5, only if the gates pass

Commit each item with its acceptance test. Maintain `MEASUREMENT_LOG.md` recording the pip and R
delta from each correctness fix.
