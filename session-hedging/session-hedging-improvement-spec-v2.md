# `session-hedging`: Improvement and Implementation Specification, v2

> **Supersedes v1.** This revision folds in an independent deep-research report that ran a
> six-month M1 pilot on the actual strategy logic. That pilot supplies numbers v1 could only
> guess at, and it changes several of v1's recommendations. Changes are marked **[v2]** with a
> short note on what moved and why.
>
> **Audience.** Claude Code, working in the `session-hedging` repository, with `strategy.md`
> (current implementation spec) as the description of existing behaviour.
>
> **Prime directive.** Do not tune parameters before fixing measurement. The pilot demonstrated
> exactly why: the same strategy, same data, same period, scored anywhere from **+25.43R to
> −43.42R** depending purely on an intrabar assumption. Everything downstream of that is noise
> until the assumption is resolved.

---

## 0. What the pilot established, and what it changes

### 0.1 The headline numbers

An independent pilot reconstructed this exact strategy over 387 session-open pairs (Tokyo,
London, New York, 129 clean opens each) on six months of XAUUSD M1 data (1 Feb to 31 Jul 2026).

| Fill assumption | Total R | Expectancy | Pair win rate | Profit factor |
|---|---:|---:|---:|---:|
| M15, current engine ordering (optimistic) | +25.43R | +0.0657R | 33.3% | 1.109 |
| M15, newly locked stop checked first (pessimistic) | −43.42R | −0.1122R | 28.2% | 0.834 |
| M1, current engine-style ordering | +24.64R | +0.0637R | 33.3% | 1.106 |
| **M1, conservative same-minute lock recheck** | **+14.56R** | **+0.0376R** | **32.6%** | **1.062** |
| M1 conservative + $0.25/oz per order side | −4.38R | −0.0113R | 32.6% | 0.982 |
| M1 conservative + 24h max hold, no costs | +24.57R | +0.0635R | 36.2% | 1.118 |

Read the first two rows together. The **69R spread** between the optimistic and pessimistic M15
bounds is larger than the entire measured edge, and it straddles zero. The M15 engine cannot
adjudicate this strategy at all. Moving to M1 narrows the ambiguity to about 10R.

### 0.2 The cost arithmetic, in pips

This is the number that matters most, and it is worth restating in the unit this project reports
in. On gold with `pip_size = 0.1`, one pip is `$0.10/oz`.

- The pilot's break-even execution drag was about **$0.192/oz per transaction side**, which is
  **1.92 pips per side**.
- `hedge_pair` crosses the spread on **four sides** (two entries, two exits), so break-even total
  cost is roughly **7.7 pips per pair**.
- Median opening 15m range was `$9.86` to `$11.95`, so `S = 2 x range` gives a median
  **1R of roughly 200 to 240 pips**.
- Therefore the entire gross edge of this strategy is about **7.5 to 9 pips per pair**, against a
  1R of 200+ pips. Expressed as a fraction: gross expectancy `+0.0376R`, break-even cost
  `≈ 0.035R`. **The edge and the cost are the same size.**

That single line should govern every design decision below. A strategy whose gross edge equals
roughly one to two spread crossings does not have room for a redundant leg, a wide stop, or an
optimistic fill model.

### 0.3 The sharpest argument in the report [v2]

**An equal-size long and short opened at the same price have zero gross P&L until one leg closes.**
For quantity `q`: `q(P_t - P_0) + q(P_0 - P_t) = 0`, identically, at every price.

When price reaches `P_0 + S`, the short has lost `S` and the long has gained `S`. The pair is
still flat. From that instant forward, the position's incremental payoff is **exactly that of a
long opened at `P_0 + S`** with the survivor's stop and the original target.

So the simultaneous hedge generates **no gross alpha whatsoever**. It is a payoff-identical, more
expensive way of expressing a breakout entry at `entry ± S`. It pays four transaction sides to
produce what two sides produce.

This is a stronger and more precise claim than v1's "the bracket costs less". v1 proposed an OCO
bracket triggered at the **opening range edge**, which is a *different signal* with a different
payoff, so it could never isolate the cost of the hedge. **[v2]** The spec now mandates a
payoff-matched control: `synthetic_breakout`, triggering at `entry ± S`, which is what the hedge
becomes after its first stop.

The hedge is not thereby refuted. It may still earn its keep through gap behaviour, barrier
mechanics, or drawdown smoothing. But it must now beat a control that produces the identical gross
payoff at half the transaction count, and the burden of proof sits with the hedge.

### 0.4 Corrections to v1 [v2]

| v1 said | Pilot evidence | v2 says |
|---|---|---|
| Default `TIME_EXIT_MODE=session_end` | 4h exit: **−0.0290R**. 8h: +0.0339R. 24h: **+0.0635R**. 48h: +0.0620R | Default to **24h max age**. Session-end is demoted to a tested variant. Short exits destroy the edge because the 6x-range move needs hours |
| Lower `RR` because 3R looks unreachable | RR sweep is **non-monotonic**: 2.0 (+0.0949R), 2.5 (+0.0846R), 3.0 (+0.0376R), 3.5 (+0.0952R), 4.0 (+0.1012R) | Non-monotonicity over 387 samples is a **noise signature**, not a curve. Do not read a target off it. `RR=3` is not privileged, but neither is `RR=4`. This needs walk-forward, not a sweep |
| Prefer the OCO bracket on cost grounds | The payoff-matched control is `entry ± S`, not the range edge | `synthetic_breakout` is the mandatory control. `oco_bracket` remains as a separate signal variant, not as the hedge's benchmark |
| `RISK_PCT` default 0.25% | Pilot max drawdown **26.43R**. At 0.25%/R that is a **6.61%** account drawdown, breaching a 5% daily or nearing a 10% max | Default **0.10%** per one-leg R (≈2.64% historical drawdown). Range 0.075 to 0.125% |
| Model prop limits on closed-bar equity | FTMO and FundedNext both count **floating P&L, swap and commission** toward the daily limit | PropGuard must mark to market intrabar, on the M1 series |
| Session windows are broadly fine | Tokyo gold actually opens **08:45 JST** (JPX/TOCOM), not 09:00. COMEX floor reference is **08:20 ET**, not 08:00. NY expectancy was **−0.0738R** | Anchors become a first-class research parameter. The NY anchor is a prime suspect for its negative result |
| Lock mode is an open question | Breakeven lock (**+0.0543R**) beat the current `$2` / 20-pip lock (**+0.0376R**); `$5` was negative (−0.0138R) | Default `LOCK_MODE=breakeven`. The absolute 20-pip lock has no evidential support |
| Max 2 concurrent structures | Pilot reached **6** concurrent pairs from only 3 daily signals, because unresolved pairs spill across days | Cap at **3**, plus a rule forbidding a new pair in a session while that session's prior pair is open |

### 0.5 What the pilot does not establish

Guard against over-reading it. It is **six months**, 387 pairs, on a **free, non-exchange-certified**
XAUUSD M1 sample with OHLC and tick volume but **no bid/ask**. Every parameter table in it is
**in-sample**. London's `+0.1298R` and New York's `−0.0738R` are one six-month draw, not a verdict
on either session. The 24h exit result is in-sample too. Treat the whole thing as a well-built
hypothesis generator that sets priors and rules out some options, not as validation.

---

## 1. Unit policy: performance is counted in pips [v2, new section]

Performance reporting stays in **pips**, per the project's existing convention. But the current
implementation has a latent unit bug that fixed-fractional sizing will expose, so the policy must
be written down before sizing changes.

### 1.1 The problem

`strategy.md` §4.9: `_pnl_pips` deliberately does **not** scale with `qty`. Under fixed lots that
is harmless, because every trade has the same size and raw pips are additive. Under
fixed-fractional sizing it is wrong: a wide-`S` trade gets a *smaller* position, so its pip moves
are larger but each pip is worth less. Summing raw pips across variable-size trades **overweights
wide-stop trades** and produces a P&L series that corresponds to no achievable account.

### 1.2 The policy

Every closed leg and every structure carries four figures. All are reported. Pips lead.

| Unit | Definition | Use |
|---|---|---|
| `pips_raw` | `(exit - entry) / pip_size`, signed by side, unscaled | Legacy continuity, per-leg diagnostics, MAE/MFE geometry |
| **`pips_weighted`** | `pips_raw x (qty / QTY_REF)` | **The primary additive performance series.** Equals `pips_raw` exactly under fixed lots, so nothing changes for existing runs |
| `r_multiple` | `pips_raw / (S / pip_size)` | Cross-trade comparability, sizing analysis, drawdown in R |
| `cash` | `pips_weighted x DOLLARS_PER_PIP_PER_QTY x QTY_REF` | Prop limits only, which are equity-based and cannot be expressed in pips |

- `QTY_REF` is a configured reference lot (default: current `QTY`). It makes the weighted series
  interpretable as "pips at reference size" and keeps continuity with historical reports.
- **Costs are denominated in pips.** `spread_pips`, `slippage_pips`, and converted
  `commission_pips` and `swap_pips`. The cost breakdown, the break-even cost, and the net figure
  are all pip quantities, so the §0.2 arithmetic is directly visible in the report.
- **Equity curve and drawdown** are computed on `pips_weighted` as the headline series, with a
  parallel cash series maintained solely for PropGuard.
- The existing mixed-unit `equity = initial_capital + realized + unrealized` (`strategy.md` §13.7)
  is removed or renamed `legacy_pnl_price_delta` and dropped from the UI. `POINT_VALUE` becomes
  configurable and sourced from broker contract specification, never inferred from `pip_size`.

**Acceptance criteria.**
- `test_pips_weighted_equals_pips_raw_under_fixed_lot` on the existing fixture.
- `test_pips_weighted_is_additive_under_variable_sizing`: two trades with `S` differing by 3x,
  sized fixed-fractionally, produce equal `pips_weighted` contribution for equal `r_multiple`.
- `test_cost_pips_and_cash_agree` to the cent through the configured bridge.
- Report header states `QTY_REF`, `pip_size`, and `DOLLARS_PER_PIP_PER_QTY`.

---

## 2. Target architecture

```
src/
├── engine.py          # orchestration only: step(), state, event emission
├── entry/             # NEW: pluggable entry structures
│   ├── base.py        #   EntryMode protocol
│   ├── hedge_pair.py  #   current symmetric straddle (benchmark)
│   ├── synthetic.py   #   [v2] payoff-matched control, triggers at entry ± S
│   ├── contingent.py  #   [v2] staged hedge, ratio 0 -> h on failure
│   └── oco_bracket.py #   range-edge bracket (separate signal variant)
├── units.py           # [v2] pips_raw / pips_weighted / R / cash conversions
├── sizing.py          # stop distance and position size
├── costs.py           # spread, commission, slippage, swap, all in pips
├── fills.py           # fill model + intrabar path resolver ladder
├── filters.py         # signal-quality and regime filters
├── exits.py           # lock, partial, trail, time/age exit
├── indicators.py      # ATR, true range, NR4/NR7, rolling percentiles
├── validation.py      # candle sanity gates
├── metrics.py         # expectancy, PF, MAE/MFE, R histograms, DSR/PBO inputs
├── firm_profile.py    # [v2] machine-readable prop firm rule set
├── risk_guards.py     # exposure caps + PropGuard
├── research/          # offline studies, not importable from the serving path
│   ├── mfe_study.py, break_study.py, anchor_study.py
│   ├── walkforward.py, overfitting.py, montecarlo.py
└── models.py, sessions.py, candles.py, paper.py, notifier.py, api.py, config.py, main.py
```

**Invariant to preserve.** Backtest and paper must continue to call the identical code path. No
new module may read `datetime.now()`.

### 2.1 The `EntryMode` protocol

```python
class EntryMode(Protocol):
    name: str
    def on_signal(self, ctx: SignalContext) -> list[PendingOrder]: ...
    def on_bar(self, ctx: BarContext, state: EntryState) -> list[OrderAction]: ...
    def transaction_sides(self) -> int:   # 4 for hedge_pair, 2 for synthetic, 2-3 contingent
        ...
    def describe(self) -> dict: ...
```

All modes emit the same `Position` objects, so `exits.py`, `costs.py`, `units.py`, and `metrics.py`
never branch on mode. `transaction_sides()` exists so the cost report can attribute the structural
overhead explicitly.

---

## 3. Configuration surface

New keys marked **(new)**. All validated in `config.py`, echoed by `--validate-config`.

### 3.1 Entry structure [v2 expanded]

| Key | Type | Default | Notes |
|---|---|---|---|
| `ENTRY_MODE` **(new)** | `hedge_pair` \| `synthetic_breakout` \| `contingent_hedge` \| `oco_bracket` | `hedge_pair` | Four-way A/B axis. `hedge_pair` is the incumbent benchmark |
| `SYNTH_TRIGGER` **(new)** | `s_distance` \| `range_edge` | `s_distance` | `s_distance` is the payoff-matched control at `entry ± S` |
| `HEDGE_RATIO_INITIAL` **(new)** | float | `0.0` | For `contingent_hedge`. `1.0` reproduces `hedge_pair` |
| `HEDGE_TRIGGER_MODE` **(new)** | `none` \| `failure_zone` \| `vol_spike` | `failure_zone` | When the contingent hedge is staged |
| `HEDGE_FAILURE_K` **(new)** | float | `0.5` | Failure zone at `E + S - k * R0` after an upper break |
| `HEDGE_RATIO_STAGED` **(new)** | float | `1.0` | Ratio to move to when staged. Test 0.5 and 1.0 |
| `ORB_MINUTES` **(new)** | int | `15` | Opening-range length. Test 15 and 30 |
| `OCO_BUFFER_MODE` / `OCO_BUFFER_VALUE` **(new)** | | `atr_frac` / `0.10` | For `oco_bracket` only |
| `OCO_EXPIRY_BARS` **(new)** | int | `4` | Cancel unfilled brackets |
| `ALLOW_REENTRY` **(new)** | bool | `false` | Max one per session, tagged in events |
| `SKIP_DOJI` | bool | `true` | Unchanged |

### 3.2 Session anchors [v2 expanded]

The pilot found NY expectancy negative and simultaneously flagged that the NY anchor may be
economically wrong. Anchors become parameters.

| Key | Type | Default | Notes |
|---|---|---|---|
| `SESSION_ANCHORS` **(new)** | list of `name:TZ:HH:MM` | current three | Replaces implicit cash-window opens |
| `ANCHOR_WINDOW_MINUTES` **(new)** | int | `15` | Observation window at the anchor |
| `HOLIDAY_CALENDAR` **(new)** | path | `""` | Currently absent entirely (`strategy.md` §3) |
| `PER_SESSION_PARAMS` **(new)** | bool | `true` | Independent `SL_MULT`, `RR`, `LOCK_*`, `MAX_AGE` per session |

Anchor grid to test (from the report's market-structure review):

| Anchor | Local time | Basis |
|---|---|---|
| Tokyo (current) | 09:00 JST | Incumbent |
| **Tokyo actual open** | **08:45 JST** | JPX/TOCOM gold day session |
| London (current) | 08:00 London | LBMA market-making hours begin. Cleanest of the three |
| London AM auction | 10:30 London | LBMA Gold Price |
| London PM auction | 15:00 London | LBMA Gold Price |
| New York (current) | 08:00 ET | Incumbent, and the suspect |
| **COMEX floor reference** | **08:20 ET** | Traditional open-outcry reference |
| US data window | 08:30 ET | Tier-1 releases |
| US equity open | 09:30 ET | Alternative event |
| Sydney | broker liquidity restart | **Not** a fixed 08:00. No Sydney gold exchange open exists. Keep disabled until a broker-specific restart is identified |

### 3.3 Stop sizing

| Key | Type | Default | Notes |
|---|---|---|---|
| `STOP_MODE` **(new)** | `bar_range` \| `atr` \| `blend` | `bar_range` | Incumbent default preserved |
| `SL_MULT` | float | `2.0` | Pilot's best in-sample value. 1.0 and 1.5 were clearly negative, so the parameter is not arbitrary, but 2.5 and 3.0 were near flat |
| `ATR_PERIOD` / `ATR_MULT` **(new)** | int / float | `14` / `1.25` | For `STOP_MODE=atr` |
| `BLEND_WEIGHT` **(new)** | float | `0.5` | `S = w * bar_range + (1-w) * atr` |
| `MIN_STOP_PIPS` | float | `0` | **Must become non-zero.** Derive from live spread plus a slippage quantile, see §7.1 |
| `MAX_STOP_PCTL` **(new)** | float | `0.90` | **[v2]** Cap on a *rolling percentile* of opening range, not a fixed pip figure. 95th-pct opens were `$24` to `$33` versus `$10` to `$12` medians, a 2.5x to 2.8x risk swing under fixed lots |
| `MAX_STOP_ACTION` **(new)** | `skip` \| `clamp` | `skip` | |

### 3.4 Targets, locks, exits [v2 revised defaults]

| Key | Type | Default | Notes |
|---|---|---|---|
| `RR` | float | `3.0` | Retained as incumbent. **Do not tune from the pilot sweep**, it is non-monotonic |
| `TP_MODE` **(new)** | `fixed_r` \| `partial_trail` | `fixed_r` | |
| `PARTIAL_TP_R` / `PARTIAL_FRACTION` **(new)** | float | `1.0` / `0.5` | |
| `TRAIL_MODE` / `TRAIL_ATR_MULT` / `TRAIL_ACTIVATE_R` **(new)** | | `none` / `2.0` / `1.0` | |
| `LOCK_MODE` **(new)** | `none` \| `breakeven` \| `absolute` \| `r_relative` | **`breakeven`** | **[v2]** Pilot: BE `+0.0543R` beat `$2` `+0.0376R`; `$5` was negative. The 20-pip absolute lock has no support |
| `LOCK_PIPS` | float | `20` | Only for `LOCK_MODE=absolute` |
| `LOCK_TRIGGER_R` / `LOCK_LEVEL_R` **(new)** | float | `1.0` / `0.1` | Test `0`, `0.1R`, `0.2R` |
| `MAX_AGE_HOURS` **(new)** | float | **`24`** | **[v2] This replaces `session_end` as the default exit.** 4h was negative, 8h and 12h mildly positive, 24h and 48h best |
| `TIME_EXIT_MODE` **(new)** | `none` \| `max_age` \| `session_end` \| `n_bars` \| `clock` | `max_age` | |
| `FLAT_BEFORE_WEEKEND` **(new)** | bool | `true` | Interacts with `MAX_AGE_HOURS`; weekend gap risk is uncompensated |

**Note the tension, and do not resolve it by assertion.** The 6x-range move the `3R` target
requires is reached (in either direction) by only 0.0 to 6.2% of sessions within 1 hour, but 52.7
to 62.0% within 24 hours. A short time exit therefore truncates the strategy's only paying
outcome. That is why 4h exits lose money. The tension between "cut holding risk" and "the target
needs a day" is real, and the resolution is either a lower target, a partial exit, or accepting
overnight exposure with its swap and prop-rule consequences. Measure all three.

### 3.5 Risk and sizing [v2 revised]

| Key | Type | Default | Notes |
|---|---|---|---|
| `RISK_MODE` **(new)** | `fixed_qty` \| `fixed_fractional` | `fixed_qty` | Incumbent preserved for parity testing |
| `RISK_PCT_PER_R` **(new)** | float | **`0.10`** | **[v2]** Percent of equity per one-leg R. Pilot's 26.43R drawdown implies ~2.64% at this level. Research range 0.075 to 0.125. For a 3%-daily programme, 0.075 to 0.10 |
| `MAX_PAIR_RISK_PCT` **(new)** | float | `0.20` | Reserve for the `-2R` whipsaw |
| `MAX_OPEN_RISK_PCT` **(new)** | float | **`0.75`** | Aggregate cap across all open structures |
| `MAX_CONCURRENT_STRUCTURES` **(new)** | int | **`3`** | **[v2]** Pilot reached 6 |
| `ONE_OPEN_PER_SESSION` **(new)** | bool | **`true`** | **[v2]** Forbid a new pair while that session's prior pair is open. This is what produced the 6-way stack |
| `QTY_REF` **(new)** | float | current `QTY` | Reference lot for `pips_weighted` |
| `CONTRACT_SIZE` / `POINT_VALUE` **(new)** | float | `100` / configurable | From broker contract spec, never inferred from `pip_size` |

### 3.6 Costs, in pips [v2]

| Key | Type | Default | Notes |
|---|---|---|---|
| `COST_MODEL` **(new)** | `none` \| `fixed` \| `per_session` \| `from_candle` \| `from_tick` | `per_session` | |
| `SPREAD_PIPS_DEFAULT` **(new)** | float | `2.0` | 2.0 pips = `$0.20/oz` |
| `SPREAD_PIPS_TOKYO` / `_LONDON` / `_NEW_YORK` **(new)** | float | | Tokyo and rollover are materially wider |
| `SPREAD_NEWS_MULT` **(new)** | float | `3.0` | |
| `COMMISSION_PER_LOT_PER_SIDE` **(new)** | float | `3.5` | Converted to pips for reporting |
| `SLIPPAGE_MODEL` / `SLIPPAGE_PIPS` **(new)** | | `fixed_pips` / `1.0` | Stop fills and gap fills only, never favourable |
| `SWAP_LONG_PER_LOT` / `SWAP_SHORT_PER_LOT` **(new)** | float | broker-sourced | Both legs accrue in `hedge_pair`. With a 24h default hold this now bites |
| `TRIPLE_SWAP_WEEKDAY` **(new)** | int | `2` | |
| `BREAKEVEN_COST_REPORT` **(new)** | bool | `true` | Emit the break-even cost in pips per side and per structure every run |

### 3.7 Measurement

| Key | Type | Default | Notes |
|---|---|---|---|
| `INTRABAR_MODE` **(new)** | `optimistic` \| `pessimistic` \| `m1` \| `m1_conservative` \| `tick` | **`m1_conservative`** | **[v2]** The pilot's preferred specification: when a newly established survivor stop and the target both fall inside the same minute, the stop takes precedence |
| `M1_CACHE_DIR` **(new)** | str | `data/candles` | |
| `TICK_SOURCE` **(new)** | str | `""` | Reserved for the bid/ask tier |

---

## 4. Phase 0: Measurement correctness (blocking)

### W0.1: Intrabar path resolver ladder [v2 expanded]

**Problem.** `strategy.md` §4.8, §13.5. Branch B applies the lock and checks the survivor's
original TP on the same bar without re-checking the survivor's new stop.

**Evidence of severity.** The pilot's two opposite M15 bounds differ by **69R** and straddle zero.
This is not a hygiene issue; it determines the sign of the result.

**Change.** Extract level-crossing into `src/fills.py` behind a resolver interface, with a ladder
of increasing realism (the report's `OHLC-conservative → M1 path proxy → bid/ask tick →
order-book/latency` progression):

```python
class PathResolver(Protocol):
    tier: int
    def resolve(self, interval: Interval, levels: list[Level]) -> list[Touch]: ...
```

| Tier | Resolver | Rule |
|---|---|---|
| 0 | `OptimisticResolver` | Current behaviour. Retained **only** to measure the bias |
| 1 | `PessimisticResolver` | Adverse level always taken first within a bar |
| 2 | `M1Resolver` | Walk M1 bars in order; engine ordering inside a minute |
| 3 | **`M1ConservativeResolver`** | **Default.** As tier 2, but when a newly established survivor stop and the target both fall inside the same minute, the **stop takes precedence** |
| 4 | `TickResolver` | Bid/ask tick sequence. Interface defined now, implementation deferred |

Missing M1 for an interval falls back to tier 1 with a logged warning and a counted fallback.

**Acceptance criteria.**
- `INTRABAR_MODE` selects the tier; the tier name and fallback count appear in every report header.
- `test_branch_b_locked_stop_rechecked_same_bar` proves tiers 1, 3 book the lock exit, not a TP win.
- `test_m1_conservative_prefers_stop_on_same_minute_collision`.
- A single command produces the full ladder comparison table for a date range, in pips and R.
- **Calibration gate:** re-running the pilot period must reproduce the ladder's rough shape
  (tier 0 strongly positive, tier 1 strongly negative, tier 3 modestly positive). If it does not,
  the discrepancy is investigated before any other work proceeds.

### W0.2: Warmup, removing the spurious first signal

**Problem.** `strategy.md` §13.6. Fresh engine has `prev_in_session` all `False`.

**Change.** `ClosedBarEngine.warmup(bars)` performs membership-edge bookkeeping and indicator
seeding without arming signals. `run()` consumes `WARMUP_BARS = max(ATR_PERIOD, 2) + 1` before
live stepping. Paper's `observe()` folds into the same method.

**Acceptance.** `test_backtest_starting_mid_session_arms_no_signal`; report exposes
`warmup_bars_consumed` and the first evaluated bar timestamp.

### W0.3: Unlocked single-leg stop skip

**Problem.** `strategy.md` §13.3. Guards are `long_hit_sl and pair.short_open` and the mirror, so
an unlocked pair with one open leg falls through to the TP arms.

**Change.** Restructure `_manage_pairs` so leg exit evaluation is unconditional and the lock is a
side effect that immediately refreshes live levels:

```
for structure in open_structures:
    touches = resolver.resolve(interval, structure.levels())
    for touch in touches:            # already in path order
        apply(touch)
        if structure.levels_changed:
            structure.refresh_levels()   # new stop live for the REST of this interval
```

**Acceptance.** `test_unlocked_single_leg_stop_is_processed`. The dead `elif *_hit_tp` arms are
removed, not merely unreachable.

### W0.4: Candle validation gate

`src/validation.py`: `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`,
strictly increasing and grid-aligned `ts`, no duplicates, gap detection against the session
calendar, non-negative volume, `spread >= 0` where present.
`ON_INVALID_CANDLE = reject | drop | warn`, default `reject` for backtest, `drop` with a counter
for paper. Note the pilot's feed had no inversions or duplicates but **did** have non-trivial gaps
around closures and rollovers, so gap reporting is the part that will actually fire.

**Acceptance.** `test_validation_catches_inverted_bar`, `test_validation_catches_duplicate_ts`,
`test_validation_detects_gap`; gaps reported with count and duration per session.

### W0.5: Metric set [v2, pip-denominated]

`src/metrics.py`, per run and per session and per weekday, all pip figures using
`pips_weighted`:

`expectancy_pips`, `expectancy_r`, `profit_factor`, `win_rate_excl_be`, `be_rate`,
`r_multiple_histogram`, `mae_pips` / `mfe_pips` and their R-normalised forms,
`time_in_trade` distribution (median and 95th percentile, the pilot showed **5.27h median,
63.1h p95**), `concurrent_structures` distribution, `payoff_ratio`, `sharpe` and `sortino` on the
daily `pips_weighted` series, `max_drawdown_pips`, `max_drawdown_r`, `max_drawdown_pct`,
`longest_flat_period`, and `transaction_sides_total`.

Report both `win_rate` (legacy, breakevens in denominator) and `win_rate_excl_be`, labelled. The
strategy manufactures breakevens by design, so the difference is material.

### W0.6: Drawdown that means something [v2]

- Headline drawdown on the `pips_weighted` series, plus `max_drawdown_r` for cross-config
  comparison (the pilot's 26.43R is the reference figure).
- **Sample the equity series on M1**, not on M15 closes. Prop daily limits are equity-based and
  intrabar, so a closed-bar series understates the worst moment. Report both
  `max_drawdown_closed_bar` and `max_drawdown_intrabar`.
- Persist `equity_peak`, `max_drawdown`, and the daily-loss tracker in `snapshot()`
  (`strategy.md` §13.8).

---

## 5. Phase 1: Costs and risk (blocking for Phase 3)

### W1.1: Cost model, denominated in pips [v2]

```python
class CostModel:
    def entry_cost_pips(self, side, session, bar) -> float
    def exit_cost_pips(self, side, session, bar, is_stop: bool) -> float
    def overnight_cost_pips(self, positions, date) -> float
```

- Spread charged on **every** crossing. `transaction_sides()` from the entry mode drives the
  count: 4 for `hedge_pair`, 2 for `synthetic_breakout`, 2 or 3 for `contingent_hedge`.
- `from_candle` mode reads the `spread` field already on `Candle` and currently ignored
  (`strategy.md` §2), falling back with a counter when sparse.
- Slippage on stop and gap fills only, never favourable.
- Swap accrues per position per night, both legs in `hedge_pair`, triple on the configured weekday.
  With `MAX_AGE_HOURS=24` this is now a routine cost, not an edge case.

**Required report output, every run:**

| Field | Unit |
|---|---|
| `gross_pips`, `net_pips` | pips |
| `spread_pips`, `commission_pips`, `slippage_pips`, `swap_pips` | pips |
| `cost_pips_per_structure` | pips |
| `cost_as_fraction_of_R` | ratio |
| **`breakeven_cost_pips_per_side`** | pips |
| `transaction_sides_total` | count |

The break-even figure is the project's single most decision-relevant number. The pilot put it at
**about 1.92 pips per side**. Publish it prominently and track it across every configuration.

**Acceptance.** `test_hedge_pair_charges_four_spread_crossings`,
`test_synthetic_charges_two_crossings`, `test_triple_swap_on_configured_weekday`,
`test_stop_fill_slips_adversely`, `test_net_equals_gross_minus_costs` to the pip.

### W1.2: Fixed-fractional sizing [v2 revised]

```python
def position_size(equity, risk_pct_per_r, stop_distance_price, contract_size, point_value,
                  slippage_allowance_price) -> float:
    risk_cash = equity * risk_pct_per_r / 100
    per_unit_risk = (stop_distance_price + slippage_allowance_price) * contract_size * point_value
    return risk_cash / per_unit_risk
```

- **Include a stop-slippage allowance in the denominator.** Sizing to the nominal stop
  systematically under-reserves.
- `point_value` and `contract_size` come from broker contract specification, never from `pip_size`.
- Lot rounding to broker step; a computed size below minimum lot **skips with an event**, never
  rounds up.
- `MAX_STOP_PCTL` uses a rolling percentile of opening range rather than a fixed pip cap, because
  the 95th percentile opening range was 2.5x to 2.8x the median.
- Enforce `MAX_CONCURRENT_STRUCTURES`, `ONE_OPEN_PER_SESSION`, `MAX_OPEN_RISK_PCT` before arming.

**Acceptance.** `test_fixed_fractional_risk_is_constant_across_stop_widths`,
`test_size_below_min_lot_skips_with_event`, `test_one_open_per_session_blocks_stacking`. Report
includes the realised `risk_pct_actual` distribution per structure.

### W1.3: Firm profile and PropGuard [v2 substantially revised]

**`src/firm_profile.py`.** A machine-readable rule set, not hard-coded modes:

```yaml
name: example_firm_2step
daily_loss_pct: 5.0
daily_loss_basis: equity          # includes floating P&L, swap, commission
daily_reset_tz: Europe/Prague
max_loss_pct: 10.0
max_loss_mode: static             # or trailing_eod
hedging_same_account: true
hedging_cross_account: false
correlated_instrument_hedge: false
leverage_xau: 50                  # Standard; Swing programmes are typically lower
weekend_holding: false            # Standard accounts often restrict; Swing permits
rollover_holding: false
news_restriction_minutes: 2
min_trading_days: 4
consistency_rule: null
```

**PropGuard requirements:**
- Daily loss computed on **equity including floating P&L, swap and commission**, marked on the M1
  series, not on closed trades. Both FTMO and FundedNext count floating P&L; a closed-bar guard
  would miss real breaches.
- Continuously compute **remaining headroom**, and block new entries when projected open risk
  would consume it.
- `PROP_GUARD_MODE = observe | enforce` so constrained and unconstrained expectancy are both
  measurable.
- **Internal soft stop at roughly 40 to 50% of the firm's hard daily limit**, leaving room for
  slippage, floating losses and rollover.
- Report `days_breached`, `worst_day_pct`, `min_free_margin`, `best_day_share_of_profit`.
- `MAX_AGE_HOURS=24` interacts with `weekend_holding` and `rollover_holding`; the guard must
  force-flatten where the profile forbids the hold.

This is a simulation of a rule shape. The user confirms actual rules with the firm they engage.
Note that the report found firms differ materially: FundedNext explicitly permits same-account
opposite positions, The5ers specifies an MT5 Hedge platform with a news restriction window, and
FTMO's gold leverage differs between Standard and Swing programmes. The profile exists so that
switching firms is a config change, not a code change.

---

## 6. Phase 2: `ENTRY_MODE` and the mandatory control

### W2.1: Extract the incumbent into `hedge_pair`

Pure refactor. Behaviour must be bit-identical under
`ENTRY_MODE=hedge_pair, STOP_MODE=bar_range, TP_MODE=fixed_r, LOCK_MODE=absolute,
TIME_EXIT_MODE=none, COST_MODEL=none, RISK_MODE=fixed_qty, INTRABAR_MODE=optimistic`.

**Acceptance.** Golden-file test on the committed fixture asserting identical trades, events and
stats between old and new engines. **Do not proceed until this passes.**

### W2.2: `synthetic_breakout`, the payoff-matched control [v2, new and mandatory]

**This is the most important new work item in v2.**

Same signal, same `S`, same survivor stop and target. But instead of opening both legs at
`entry`, place a single stop order at `entry + S` (long) and `entry - S` (short), OCO. Whichever
triggers becomes the position. Stop and target are the survivor's, exactly as the hedge would have
had them.

**Why it must exist.** §0.3 proves the hedge's gross payoff after its first stop is identical to
this. So any measured difference between `hedge_pair` and `synthetic_breakout` is attributable
**entirely** to transaction costs, barrier mechanics, gap behaviour, and same-bar path effects.
That is precisely the question "is the hedge worth it" reduced to a measurement.

**Expected result from the pilot's arithmetic.** At `$0.25/oz` per side, `hedge_pair` scored
`−0.0113R` across four sides; the same drag on two sides leaves roughly `+0.013R`. The control is
expected to win on cost. The hedge must produce an offsetting benefit somewhere else, or it is
retired to benchmark status.

**Acceptance.**
- `test_synthetic_payoff_matches_hedge_after_first_stop` on a constructed path where no gap or
  same-bar collision occurs: gross R must match to tolerance.
- `test_synthetic_charges_half_the_transaction_sides`.
- `test_synthetic_gap_through_trigger_fills_at_open`.
- Mode comparison report showing gross pips, net pips, cost pips, and the difference decomposed.

### W2.3: `contingent_hedge` [v2, new]

The report's preferred linear-hedge candidate, and the one that keeps hedging as a purpose-built
loss-control mechanism rather than a permanent paid-for offset.

State machine: on an upper breakout, establish the long primary with hedge ratio `0`. Stage the
hedge only if price re-enters a failure zone (`below E + S - HEDGE_FAILURE_K * R0`) or if
short-horizon realised volatility or spread exceeds a threshold. Ratio moves from `0` to
`HEDGE_RATIO_STAGED` (test `0.5` and `1.0`). Mirror on a lower breakout.

`hedge_pair` is the special case where the ratio starts at `1.0` and drops to `0` at the first
stop. `synthetic_breakout` is the case where it is always `0`. Implementing the general form makes
all three one code path with different parameters, which is both cleaner and makes the comparison
exact.

**Acceptance.** `test_contingent_ratio_zero_equals_synthetic`,
`test_contingent_ratio_one_at_entry_equals_hedge_pair`, `test_hedge_stages_on_failure_zone_entry`.

### W2.4: `oco_bracket` [v2 reframed]

Retained, but **reframed**: this is a *different signal* (trigger at the opening-range edge plus a
buffer, not at `entry ± S`), so it is a strategy variant to test on its merits, not the hedge's
control. Stop and target computed from the **fill price**. `OCO_EXPIRY_BARS` cancels stale
brackets. Optional single re-entry, tagged.

### W2.5: Mode comparison harness

One command, same range, same costs, same resolver, producing for all four modes: net pips, gross
pips, cost pips, expectancy in pips and R, profit factor, `win_rate_excl_be`, max drawdown in pips
and R, break-even cost per side, transaction sides, median and p95 hold time, max concurrent
structures, and unresolved-structure count.

---

## 7. Phase 3: Strategy redesign

Begin only after Phases 0 to 2 are green and Phase 4's first studies have run.

### 7.1 Minimum stop from the cost model, not a guess [v2]

`MIN_STOP_PIPS` must exceed a multiple of the live spread plus a slippage quantile. A stop
narrower than round-trip cost is a fee, not a stop. Compute it from `costs.py` per session rather
than configuring a constant, and emit the derived floor in the report.

### 7.2 Stop sizing

Replace the one-bar range with a smoothed estimator (`ATR`, or Yang-Zhang / Garman-Klass in
`indicators.py`, which use OHLC more efficiently). `blend` at `0.5` keeps responsiveness while
damping outliers. Apply `MAX_STOP_PCTL` skipping. Note that `SL_MULT=2.0` was the pilot's best
in-sample value and that 1.0 and 1.5 were clearly negative, so this parameter appears to carry
real information, unlike `RR`.

### 7.3 Targets [v2 corrected]

v1 argued for lowering `RR` on the grounds that 6x-range moves are rare. The pilot refines this:
they are rare **within hours** and common **within a day**. So the target is not unreachable, it is
slow. The design question is therefore not only "what target" but "what target, at what holding
horizon, at what swap cost, under what prop holding rules".

Test as a matrix, not as separate sweeps: `RR x MAX_AGE_HOURS x TP_MODE`. Include
`partial_trail` so a shallow-but-common excursion can be monetised while the runner waits for the
tail. Do not read a target off the pilot's non-monotonic `RR` sweep.

### 7.4 Locks

Default `LOCK_MODE=breakeven`. Test `none`, `breakeven`, `r_relative` at `0.1R` and `0.2R`, and
`absolute` at the incumbent 20 pips for continuity. Expect `absolute` to lose. Report win rate
alongside expectancy so the win-rate-versus-expectancy trade-off that breakeven stops always
produce is visible rather than implicit.

### 7.5 Filters

`bullish` is computed and ignored for execution (`strategy.md` §4.4). The pilot supports that
caution: opening-bar direction predicted the direction 60 minutes after entry only **51.9% Tokyo,
46.5% London, 48.8% New York**. Do not promote it to a size tilt without a validated directional
feature.

Filters worth testing, each individually toggleable and individually attributed
(`trades_skipped_by_filter`): higher-timeframe trend bias; NR4/NR7 contraction preconditions;
`FILTER_MIN_RANGE_ATR` / `FILTER_MAX_RANGE_ATR`; spread and depth gates at the anchor; news
blackout windows. Session selection is itself a filter, but do **not** hard-drop New York on the
pilot's single negative six-month draw; test the anchor first, since 08:00 ET may simply be the
wrong event.

---

## 8. Phase 4: Research harness

Offline, under `src/research/`, writing JSON plus rendered markdown.

### S1: Conditional target-hit study [v2 sharpened]

The pilot supplied **upper-bound any-direction reach frequencies** for 6x the opening range:

| Session | within 1h | 4h | 8h | 24h |
|---|---:|---:|---:|---:|
| Tokyo | 0.8% | 7.0% | 20.2% | 56.6% |
| London | 0.0% | 7.0% | 41.1% | 62.0% |
| New York | 6.2% | 24.0% | 31.8% | 52.7% |

These are **not** target-hit probabilities. The strategy additionally requires the opposite leg to
have stopped, the survivor to survive its lock, and the *correct* direction to reach the target.

S1 must therefore compute the **conditional** version: `P(survivor reaches kR | first stop
occurred, lock survived)`, by session, by holding-horizon bucket, and by ATR regime tercile, for
`k = 1, 1.5, 2, 2.5, 3, 4`, using M1. Also produce the MFE and MAE distributions in both pips and
opening-range units. This is what actually selects `RR`.

### S2: Single-break versus double-break frequency

After the opening range is established, how often does price break one side and never test the
other? By session, weekday, contraction regime. Quantifies the `-2R` whipsaw for `hedge_pair` and
the false-break rate for `oco_bracket`.

### S3: Anchor study [v2, new]

Run the identical strategy across the §3.2 anchor grid, per session. Report expectancy in pips and
R, range and tick-volume expansion versus the preceding window, and spread behaviour where data
permits. **Primary question: is New York's negative result an anchor problem?** The pilot's
expansion ratios were Tokyo 1.415x, London 1.324x, New York 1.250x on range, and 1.886x, 1.256x,
1.276x on tick volume, so the anchors do mark real events; the question is whether they mark the
*right* ones.

### S4: Cost sensitivity and break-even, in pips

Sweep spread, slippage, commission per mode. Report break-even cost in pips per side and the ratio
of realised edge to modelled cost. **Release gate needs at least 2x headroom**, and the pilot's
1.0x is the reason.

### S5: Resolver ladder bias quantification

Run identical config across all resolver tiers. Report structures whose outcome changed and the
total pip and R delta. Publish in the README as the project's calibration constant. Expected shape
from the pilot: tier 0 ≈ +25R, tier 1 ≈ −43R, tier 3 ≈ +15R over a comparable period.

### S6: Nested walk-forward

Freeze a training window, choose parameters on it alone, test the immediately following unseen
period, roll forward, aggregate **only unseen results**. Session anchor, `SL_MULT`, `RR`,
`LOCK_MODE`, `MAX_AGE_HOURS`, hedge ratio and entry mode are all model parameters and all must be
inside the loop. Keep a final untouched holdout unavailable until the protocol is frozen.

Compute the deflated Sharpe ratio and the probability of backtest overfitting (CSCV). **Log every
configuration evaluated, not just the winners**, or these statistics cannot be computed honestly.

### S7: Prop-account Monte Carlo [v2, new]

Resample **complete trade clusters**, not individual legs, preserving London/NY overlap and
volatility-regime clustering. Simulate spread and slippage tails, gap stops, and concurrent
exposure. Outputs that matter: probability of violating the 3% or 5% daily limit, probability of
violating the 6% or 10% maximum-loss limit, expected days to breach, expected time to target, and
the distribution of minimum free margin. For a prop account these path statistics are decisive in
a way that Sharpe is not.

---

## 9. Decision gates [v2 updated with pilot priors]

Written before the studies run, so analysis cannot drift into rationalisation.

| Question | Evidence | Gate |
|---|---|---|
| Does the hedge earn its extra two transaction sides? | W2.5, `hedge_pair` vs `synthetic_breakout`, net pips | If the hedge does not beat the control on net expectancy **or** materially reduce breach probability in S7, retire it to benchmark status and deploy the control |
| Is there enough cost headroom? | S4 break-even pips per side vs the broker's measured spread | Require **at least 2x** headroom. The pilot's ~1.0x is a fail |
| What `RR`? | S1 conditional hit rates, not the pilot's non-monotonic sweep | Choose from the conditional distribution crossed with `MAX_AGE_HOURS`. Treat any sweep-derived optimum with an inconsistent neighbourhood as noise |
| Does the lock help? | `LOCK_MODE` sweep under walk-forward | Expect `breakeven` or `none` to win. `absolute` 20 pips carries no support and should not survive |
| Is New York broken, or is its anchor wrong? | S3 | Do not disable a session on one six-month draw. Test 08:20 and 09:30 first |
| What holding horizon? | `RR x MAX_AGE_HOURS x TP_MODE` matrix, plus swap cost in pips, plus firm profile holding rules | The 24h prior is in-sample. Confirm out-of-sample, and price the swap |
| Is the edge real at all? | S6 out-of-sample degradation, deflated Sharpe, PBO | Positive expectancy in **most folds**, not only in aggregate; no single session supplying nearly all profit; a **broad** profitable parameter neighbourhood, not a narrow optimum |
| Is it prop-survivable? | S7 breach probabilities under the firm profile | Breach probability comfortably below the hard limits, with the internal soft stop active |

---

## 10. Phase 5: Hygiene and live-readiness

- **W5.1 Bounded state.** Prune `pairs` and `events` (`strategy.md` §13.1, §13.2). `snapshot()`
  must not serialise unbounded history each tick.
- **W5.2 Restore robustness.** Fix `primary_side is None` on restored snapshots (§13.4). Version
  the snapshot schema and migrate explicitly.
- **W5.3 Silent failures.** `sl_dist <= 0` emits an event and counter (§13.9). Paper gap handling
  warns rather than silently dropping bars older than `last_ts` (§13.12). `fetch_range` accounts
  for weekends and closures (§13.10).
- **W5.4 Concurrency.** Move backtests off the serving loop (§13.14); paper must never be starved
  by a research run.
- **W5.5 Paper execution realism [v2].** Paper must stop retrospectively "filling" at an
  already-known bar open. It should record an order when the signal becomes actionable and measure
  the difference against the observable executable price, storing requested price, local send
  timestamp, acknowledgement timestamp, broker timestamp and fill timestamp. Realised slippage per
  session then feeds directly back into `costs.py`. This converts paper mode from a plumbing test
  into an execution measurement, which `strategy.md` §14 correctly identifies as its current gap.
- **W5.6 MT5 path.** Platform is MT5 in hedging mode. Symbol metadata (contract size, tick size,
  min and step lot, swap rates, rollover hour) sourced from the broker, never hard-coded. Before
  live: order state machine with restart idempotency, broker reconciliation refusing to trade on
  mismatch, manual and automatic kill switch, live-versus-paper divergence monitor.
- **W5.7 M1 and tick seeding.** `--seed` supports M1 alongside M15, with `--verify` running
  `validation.py` over the cache. Document the cache size for the intended history depth. Define
  the interface for a later bid/ask tick tier (broker XAUUSD ticks, and optionally COMEX GC/MGC
  trades and top-of-book) without implementing it yet.

---

## 11. Testing requirements

- **Golden-file parity test** (W2.1) in CI. It is the safety net for the entire refactor.
- **Equivalence tests** across modes: `contingent(ratio=0) == synthetic`,
  `contingent(ratio=1 at entry) == hedge_pair`, and
  `synthetic == hedge_pair` gross after first stop on collision-free paths.
- **Property tests** for `fills.py`: no fill better than its level, no double-close, no touch
  sequence inconsistent with the interval's OHLC bounds.
- **Unit-policy tests** per §1.2.
- **Determinism**: identical inputs and config produce byte-identical reports.
- **Config matrix smoke test** across `ENTRY_MODE x STOP_MODE x TP_MODE x LOCK_MODE x
  INTRABAR_MODE`.
- **Cost invariant**: `net_pips == gross_pips - total_cost_pips` exactly.
- Keep the existing behavioural pins: `test_ny_first_bar_uses_open_not_previous_close`,
  `test_gap_through_stop_fills_at_open`, `test_be_bucket_from_fill_not_bar_close`,
  `test_both_stops_same_bar_no_lock`.

---

## 12. Reporting and UI

Report header echoes: `ENTRY_MODE`, `SESSION_ANCHORS`, `STOP_MODE`, `TP_MODE`, `LOCK_MODE`,
`TIME_EXIT_MODE`, `MAX_AGE_HOURS`, `RISK_MODE`, `COST_MODEL`, `INTRABAR_MODE` and resolver tier,
`QTY_REF`, firm profile name, date range, warmup bars, validation summary, M1 fallback count.

Client additions:
- **Pips lead**: net pips, gross pips, cost pips, expectancy in pips. R shown alongside.
- Break-even cost per side, prominently, with the broker's configured spread beside it.
- Both `win_rate` and `win_rate_excl_be`, labelled.
- R-multiple histogram, MAE/MFE scatter, holding-time distribution (median and p95),
  concurrent-structures timeline.
- Per-session and per-weekday tables.
- Prop panel: worst simulated day, days breached, min free margin, headroom over time.
- Four-mode comparison view.

---

## 13. What this spec is not confident about [v2 expanded]

- **The pilot is six months, 387 pairs, on a non-certified free feed with no bid/ask.** Every
  parameter table in it is in-sample. It sets priors; it settles nothing.
- **`RR` is genuinely unresolved.** The non-monotonic sweep (2.0 good, 3.0 dip, 3.5 and 4.0 good)
  is what noise looks like. Anyone reading `RR=4` off that table is fitting.
- **Session results are one draw.** London `+0.1298R` and New York `−0.0738R` are not verdicts.
  The NY anchor question must be tested before the session is judged.
- **The 24h holding prior is in-sample** and interacts with swap costs and firm holding rules that
  the pilot did not price.
- **Whether the hedge beats its control is unknown**, though the cost arithmetic is against it.
  The control is mandatory precisely because the question is open.
- **Cost figures are placeholders** until broker bid/ask ticks replace them. `$0.192/oz` is a
  break-even stress result, not a spread quote.
- **Prop rules vary by firm, programme and date**, and change. The firm profile exists so this is
  configuration. The user confirms actual rules with the firm they engage.
- **It remains possible that nothing survives.** A gross profit factor of 1.062 with a break-even
  cost equal to the gross edge is not a strategy yet; it is a signal that might become one after
  the hedge overhead is removed, the anchors are corrected, and the risk model is rebuilt. §9
  exists so that a negative result is recognised as a result.

---

## 14. Execution order for Claude Code

1. W0.2, W0.4, W0.3 (cheap, self-contained, unblock clean runs)
2. **W5.7 M1 seeding** (blocks W0.1)
3. **W0.1 resolver ladder**, then run **S5** and check against the pilot's expected shape
4. §1 unit policy (`units.py`), then W0.5, W0.6
5. W1.1 costs, W1.2 sizing, W1.3 firm profile and PropGuard
6. W2.1 parity gate, then **W2.2 synthetic control**, W2.3 contingent, W2.4 bracket, W2.5 harness
7. S1, S2, S3, S4
8. Phase 3, driven by study outputs and the §9 gates
9. S6 walk-forward, S7 Monte Carlo
10. Phase 5 remainder, only if the gates pass

Commit each work item with its acceptance test. Maintain `MEASUREMENT_LOG.md` recording the pip
and R delta each correctness fix produces. That log is the honest history of how much of the
original result was real.
