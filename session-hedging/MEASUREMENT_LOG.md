# Measurement log

Record the pip and R delta of each Phase 0 correctness fix. Do not use this file to retune
parameters. A cell is comparable only when `BAR_TIMEFRAME`, `ORB_MINUTES`, `ENTRY_DELAY_MINUTES`,
and `ANCHOR_TOLERANCE_MINUTES` match.

## W0.0 Anchor drift detection

**Change.** Session signals are now anchored to explicit cash-open times. The opening range is
measured over `ORB_MINUTES` from the anchor (not one bar). Entry waits until
`max(anchor + ORB_MINUTES, anchor + ENTRY_DELAY_MINUTES)`. If the first bar in the window opens
more than `ANCHOR_TOLERANCE_MINUTES` after the anchor, the signal is skipped
(`signal_skipped_anchor_drift`).

**Reconstructed legacy M15 cell.** `ORB_MINUTES=15`, `ENTRY_DELAY_MINUTES=15`,
`ANCHOR_TOLERANCE_MINUTES=15` on M15 matches the previous one-bar range and fill-at-next-open
behaviour. Existing engine tests run this cell.

**Service default (spec §1.5).** `ORB_MINUTES=60`, `ENTRY_DELAY_MINUTES=15`. That is a different
strategy cell, not a measurement of the old engine.

**H4.** Broker-style H4 bars (opens 01:00 UTC) produce zero signals under the default 15-minute
tolerance. Those runs are void, not underperforming.

**Pip / R delta.** Not comparable across the default 60-minute ORB vs the old one-bar ORB. On the
reconstructed 15/15 M15 cell the fill geometry of the existing unit tests is unchanged
(entry still at the next bar's open after the 15-minute range bar).

## W0.2 Warmup / spurious first signal

**Change.** `ClosedBarEngine.run` marks any session-day whose ORB window has already closed as
done before the first bar is stepped. A date range that starts mid-session no longer treats that
first in-window bar as the cash open.

**Pip / R delta.** Removes the fake first pair on short date-ranged backtests that begin inside a
session. Existing tests that include a pre-session bar are unchanged.

## W0.4 Candle validation

**Change.** `src/validation.py` rejects inverted OHLC, duplicate or backward timestamps, and
intervals that are not a multiple of the bar; those bars are skipped and never fill. Gaps of more
than two missing bars emit `bar_skipped_invalid` but the arriving bar is still processed — the hole
is missing data, not a corrupt print. Weekend/close gaps are allowed.

**Pip / R delta.** No change on clean fixtures. A corrupt bar can no longer stop or fill a pair.

## W0.3 Unlocked single-leg stop skip

**Change.** Branch B required the other leg to still be open before honoring a stop. An unlocked
restored pair with one leg already closed could ignore the survivor's stop on that bar. Stops on
whatever leg is still open are now always processed; the lock only applies when the hedge remains.

**Pip / R delta.** Realized P&L on the reachable restored-state / `rr < 1` path now includes the
survivor stop instead of leaving the leg open through the level.

## W5.7 M1 seeding

**Change.** `--seed-m1` writes `data/candles/<SYMBOL>/M1.jsonl` (20_000 bars by default).
`--seed --timeframe M1` remains valid. The resolver ladder in W0.1 needs this cache.

**Pip / R delta.** None (data path only).

## W0.1 Intrabar path resolver ladder

**Change.** `src/fills.py` implements optimistic / pessimistic / m1 / m1_conservative (default) /
tick (interface only). After a lock, the default re-checks the new stop on that bar and does not
take TP when both are touched. Reports `same_bar_resolution_rate` and `same_bar_r`.

**Pip / R delta.** On same-bar lock-and-target prints, conservative booking replaces the optimistic
TP. Existing engine tests keep `intrabar_mode=optimistic` so their geometry is unchanged.

## §2 Unit policy, W0.5 metrics, W0.6 drawdown

**Change.** `units.py` defines pips_raw / pips_weighted / R / cash. Reports include `realized_r`
beside `realized_pips`, the TP-rate margin panel, outcome mix, and concurrency. Mixed-unit `equity`
is now `equity_pips` (no `initial_capital` plus price deltas). Drawdown peak and max persist in
paper snapshots; `max_drawdown_r` is marked alongside. M1 covering bars, when present, also sample
the equity mark.

**Pip / R delta.** Sign of `equity` on pip-mode reports no longer impersonates a cash balance.
Headline TP-rate fields are new; they do not change fills. The report header now states
`BAR_TIMEFRAME`, `ORB_MINUTES`, `ENTRY_DELAY_MINUTES`, `ANCHOR_TOLERANCE_MINUTES`, and
`anchor_drift_p50` per session. Same-bar R is split per session. `POINT_VALUE` is an explicit
config key and is never derived from `PIP_SIZE`.

M15/H1 export CSVs are now a regression fixture (`tests/fixtures/session-hedging-XAUUSD-*.csv`).
Classifier uses pair R so a `+LOCK_PIPS` survivor is a lock, not a TP. H4 export is present and
explicitly not a rate target. Those CSVs are user data and are not tracked in git; the export
regression tests skip when the files are absent from `tests/fixtures`.

## STOP_MODE (post-Phase-0 config surface)

**Change.** `STOP_MODE=bar_range|fixed_pips` with `FIXED_STOP_PIPS`. `bar_range` is unchanged and
remains the default: `S = SL_MULT × opening range over ORB_MINUTES`. `fixed_pips` sets
`S = FIXED_STOP_PIPS × PIP_SIZE`, so `S` no longer tracks session volatility and `R` is constant
across pairs. `MIN_STOP_PIPS` floors both modes. Per-request overrides are now revalidated —
`model_copy` skipped validators, so an override could break a cross-field rule (fixed stop with no
distance, `ORB_MINUTES` not a multiple of the bar) and fail silently as "no pairs opened" instead
of a 422.

**Pip / R delta.** None on the default cell: the `bar_range` stop expression is unchanged and every
existing test still measures it (the report gains two descriptive fields and no new numbers). A
`fixed_pips` run is a **different cell** — its `R` series is
not comparable to a `bar_range` run, so do not diff the two. `stop_mode` and `fixed_stop_pips` are
in the report header and `/v1/config` so the cell is identifiable.

## W1.1 Cost model

**Change.** `src/costs.py` prices spread, slippage, and commission per actual transaction side and
long/short financing per broker rollover. The configured triple weekday prices the weekend, so
Saturday and Sunday are not charged again. Session overrides are partial numeric schedules.
Reports carry gross, cost, and net pips/R together, paired gross/net drawdown, break-even pips per
side, and the §9 spread-headroom ratio. The unprefixed Phase 0 pip/R fields remain gross aliases.

**Pip / R delta.** The zero-cost configuration is exact: gross equals net to the pip and R. The
deterministic four-side acceptance cell books 200.0 gross pips / 2.00R, 7.0 cost pips / 0.07R, and
193.0 net pips / 1.93R. This is a measurement fixture, not a tuned strategy result.

**Export criterion: unverified.** The required local-only
`tests/fixtures/session-hedging-XAUUSD-{M15,H1}.csv` files are absent. The acceptance test is present
and skipped: it requires no positive M15 budget, approximately 4.7 pips/side on the H1 four-side
pair, and approximately 9.4 pips/side for the two-side control. W1.1 must not be represented as
fully fixture-verified until those files are supplied.

## W1.2 Sizing and concurrency

**Change.** `src/sizing.py` keeps `fixed_qty` as the parity mode and adds fixed-fractional sizing
from marked equity. One-R quantity includes adverse slippage on both entry and stop exit in the
denominator and is bounded by `MAX_PAIR_RISK_PCT`. `MAX_OPEN_RISK_PCT` blocks a proposed structure
without resizing existing pairs. Per-session and global concurrency gates emit
`signal_suppressed_risk`; the report carries the suppression total and reason counts.

**Pip / R delta.** With caps disabled for the parity cell, `fixed_qty` reproduces the committed M15
candle fixture exactly: 42.0 realized gross pips / 1.00R and 60.0 open gross pips / 1.428571R, with
the same two pairs, three closed legs, and two locks. A deterministic variable-size cell (+10 raw
pips at 2.0× quantity and −10 raw pips at 0.5×) reports 15.0 additive weighted pips; its raw-pip sum
would be zero. This validates aggregation and is not a tuned result.

**H1 concurrency criterion: unverified.** The local-only H1 export is absent. The counterfactual
acceptance test is present and skipped; when supplied, it applies `ONE_OPEN_PER_SESSION=true` and a
three-structure cap to the export timeline, requires observed concurrency at or below three, and
requires a nonzero suppressed-signal count. Do not claim the measured 10-to-3 reduction until that
fixture test runs.

## W1.3 Firm profile and PropGuard

**Change.** `src/firm_profile.py` defines the explicit custom profile and
`src/risk_guards.py` evaluates daily and total loss floors on marked equity including floating
P&L. A breach is sticky, emits `prop_guard_breached`, blocks new structures, and persists in the
shared engine snapshot used by paper. It never force-closes a leg or edits closed history. Daily
references reset at the first observed mark after the configured firm-local boundary.

**Pip / R delta.** `FIRM_PROFILE=none` has no fill or accounting delta. In the deterministic
floating-loss acceptance cell, an open single leg reaches −110.0 weighted pips / −0.55R and trips
the 1% daily cash limit with zero closed trades. The delta to closed gross/net history is exactly
zero; only subsequent structures are suppressed. This is guard-path evidence, not a tuned result.

## Phase 1 max-age time exit

**Change.** `src/exits.py` defines the strict max-age predicate. Age starts at the actual entry
bar open, and a surviving leg closes at the first completed bar close strictly past
`MAX_AGE_HOURS`. Stop/target levels on that bar take precedence via the intrabar resolver ladder.
`time_exit` is a distinct `outcome_mix` bucket and is never folded into lock or whipsaw.

**Pip / R delta.** `TIME_EXIT_MODE=none` is the no-change control. In the deterministic one-hour
acceptance cell, the bar closing exactly at one hour leaves the leg open; the next 15-minute close
books +10.0 gross pips / +0.10R as `time_exit`. This is execution-path evidence, not a horizon
sweep, and no `MAX_AGE_HOURS` value was tuned.

## Phase 1 H1 report (local candle cache, not export acceptance fixture)

**Cell.** `data/candles/XAUUSD/H1.jsonl`, 2,000 bars from 2026-04-20 17:00 UTC through
2026-08-20 02:00 UTC; `ORB_MINUTES=60`, `ENTRY_DELAY_MINUTES=15`, unchanged stop/target/lock and
session parameters, `RISK_MODE=fixed_qty`, `ONE_OPEN_PER_SESSION=true`,
`MAX_CONCURRENT_STRUCTURES=3`, and `MAX_AGE_HOURS=24`. There are 190 closed pairs and two open
pairs at the final mark.

| Cost cell | Gross equity pips / R | Net equity pips / R | Execution / financing cost pips | Break-even pips/side | Spread headroom | Gross / net max DD pips | Max concurrency | Suppressed signals |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Configured zero cost | −6,358.1 / −25.7068R | −6,358.1 / −25.7068R | 0.0 / 0.0 | −8.3659 | n/a (zero spread) | 9,453.0 / 9,453.0 | 3 | 70 |
| Spec lower-bound scenario: 2 pips/side spread, other rates zero | −6,358.1 / −25.7068R | −7,886.1 / −30.1958R | 1,528.0 / 0.0 | −8.3659 | −4.1830× | 9,453.0 / 10,749.0 | 3 | 70 |

All 70 suppressions are `one_open_per_session`. Outcome mix is 12.11% TP, 34.21% lock, 0% BE,
8.42% whipsaw, and 45.26% time exit. The negative break-even budget fails the 2× cost-headroom
gate before any positive cost is applied. The 2-pip scenario is the lower bound named in spec §9,
not a fitted value; financing remains zero because no broker swap rate is configured.

This cache is useful structural/report evidence but does **not** verify W1.1's historical
approximately 4.7-pip H1 acceptance target or W1.2's measured baseline concurrency of 10. Those
remain skipped until the named export CSVs are supplied.

## W2.1 `ENTRY_MODE=hedge_pair` parity gate

**Change.** The incumbent level construction now passes through `src/entry/hedge_pair.py` and is
selected explicitly by `ENTRY_MODE=hedge_pair`. `TP_MODE=fixed_r` and `LOCK_MODE=absolute` name the
existing semantics without changing them. The API, paper configuration, UI, and report expose the
mode.

**Golden evidence.** `tests/fixtures/phase1_hedge_pair_golden.json` was captured from commit
`59eaf05` on the deterministic committed M15 candle fixture under the required parity cell:
`bar_range / fixed_r / absolute / no time exit / no costs / fixed qty / optimistic`. It binds the
complete canonical report payload, ordered trades, ordered events, grouped pair order, and stats.

**Gross/net pip and R delta.** Exactly zero. The fixture remains 42.0 realized gross/net pips and
1.0 realized gross/net R, with 60.0 open gross/net pips and 1.428571R; pair, trade, event, lock, and
ordering output is byte-for-byte identical after removing the newly descriptive `entry_mode`
header field. This is a refactor gate, not a strategy result.

## W2.2 `ENTRY_MODE=synthetic_breakout`

**Change.** The payoff-matched control stages OCO stop entries at `E ± S` and fills only the chosen
side. Its absolute stop/target are the incumbent survivor's post-first-stop levels. Trigger gaps
fill at the parent-bar open; collisions use the resolver ladder. Pending entries persist in paper
snapshots, count against concurrency/risk, and cost nothing until an order actually fills.

**Constructed no-gap acceptance cell.** `E=100`, `S=10`, `RR=3`, absolute lock `L=2`, fixed
quantity, optimistic path. `hedge_pair` closes the stopped short at 110 and survivor long at 130;
`synthetic_breakout` buys at 110 and exits at 130. Both report +20.0 gross pips / +2.0R. At one pip
of spread per side, hedge execution cost is 4.0 pips across four actual sides and net is +16.0
pips / +1.6R; synthetic cost is 2.0 pips across two actual sides and net is +18.0 pips / +1.8R.

**Delta decomposition.** Gross difference: 0.0 pips / 0.0R. Execution-cost difference:
synthetic saves 2.0 pips / 0.2R. Financing difference: 0.0 in this same-day cell. Net difference:
synthetic +2.0 pips / +0.2R. Actual fills decompose as hedge entries 2 + exits 2 versus synthetic
entry 1 + exit 1; the cancelled sibling OCO contributes zero sides and zero cost. Gap and same-bar
components are zero for this constructed identity path. These are acceptance-path figures, not a
parameter-tuned historical result.

## W2.3 `ENTRY_MODE=contingent_hedge`

**Change.** The contingent mode shares the synthetic primary OCO and stages only the defined
`failure_zone` hedge. Ratio zero with staged ratio zero delegates to synthetic; ratio one delegates
to incumbent hedge-pair at `E`. Intermediate ratios retain each partial fill's quantity, episode,
actual transaction-side count, and weighted cost-side equivalent. No volatility or spread trigger
was invented.

**Endpoint evidence.** On the W2.2 `E=100`, `S=10`, `RR=3`, `L=2` path with one pip per-side
spread, initial/staged ratio `0/0` is identical to synthetic: +20.0 gross, 2.0 cost, +18.0 net pips
(+2.0R / +0.2R / +1.8R) across two actual sides. Initial/staged ratio `1/1` is identical to
hedge-pair: +20.0 gross, 4.0 cost, +16.0 net pips (+2.0R / +0.4R / +1.6R) across four sides.

**Failure and fractional evidence.** A long primary triggered at 110 stages at 105 when
`HEDGE_FAILURE_K=0.5`; both 0.5× and 1.0× staged quantities fill there, with short stop 110 and
target 70. The 0.5 initial-ratio path opens two half tranches at `E`, closes the opposite half at
110, and adds the remaining primary half at 110, producing a 105 weighted primary entry. It records
four actual fills so far versus 2.0 weighted side equivalents. These are constructed state-machine
and cost checks, not historical performance or tuning.

## W2.4 `ENTRY_MODE=oco_bracket`

**Change.** The bracket mode stages stop entries at the measured opening-range high/low plus either
an opening-range-fraction or fixed-pip buffer. The sibling is cancelled on the first resolved fill;
gaps fill at the bar open and `S`/`RR` exits are recomputed from that actual fill. Unfilled orders
expire after the configured count of eligible parent bars. Optional re-entry creates at most one
fresh order carrying `reentry_index=1`; that order cannot recursively re-enter. Pending expiry and
re-entry state survive the paper snapshot.

**Constructed acceptance cells.** With opening range `[95, 105]`, a 0.10 range buffer produces
triggers at 94/106. A long fill at 106 receives stop 96 and target 136; the mirrored short fill at
94 receives stop 104 and target 64. A gap open at 108 fills at 108 and therefore moves the exits to
98/138. With `OCO_EXPIRY_BARS=2`, one quiet eligible bar persists the order and the second cancels
it with zero fills, transaction sides, or costs. Optimistic, pessimistic, M1, and M1-conservative
tests cover both trigger collision and entry-bar exit resolution. A no-cost long target path from
106 to 136 is +30.0 gross and net pips / +3.0 gross and net R; the expiry path is 0.0 gross and net
pips / 0.0R. These are contract checks, not a historical result or parameter sweep.

## W2.5 four-mode H1 comparison

**Cell.** One command ran `data/candles/XAUUSD/H1.jsonl`: 2,000 bars from 2026-04-20 17:00 UTC
through 2026-08-20 02:00 UTC, candle fingerprint
`77c50a90e89b1865fc8fc439a18a7d172f7a1fded74111bd2956c73fa3b8fdc6`. Every mode used the same
`bar_range`, `SL_MULT=2`, `RR=3`, `ORB_MINUTES=60`, 15-minute entry delay, 24-hour max age,
fixed quantity, three-structure cap, one-open-per-session gate, and configured zero execution and
financing rates. `INTRABAR_MODE=m1_conservative` had no local M1 cache, so all four modes used its
documented conservative no-subpath fallback. No parameter was fitted or swept.

Headline gross/net is final marked equity, including unresolved structures; expectancy, profit
factor, win rate, and hold time use completed structures.

| Mode | Completed | Gross / net pips | Gross / net R | Gross / net expectancy pips (R) | Gross / net PF | Gross / net win excl. BE | TP / required | Gross / net max DD pips (R) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | 190 | −6,358.10 / −6,358.10 | −25.7068 / −25.7068 | −33.4637 / −33.4637 (−0.1353 / −0.1353) | 0.7700 / 0.7700 | 36.31% / 36.31% | 12.11% / 17.67% | 9,453.00 / 9,453.00 (34.3662 / 34.3662) |
| synthetic_breakout | 144 | −6,040.70 / −6,040.70 | −26.6002 / −26.6002 | −41.1840 / −41.1840 (−0.1842 / −0.1842) | 0.8049 / 0.8049 | 31.25% / 31.25% | 15.28% / 22.31% | 8,623.20 / 8,623.20 (32.3089 / 32.3089) |
| contingent_hedge | 142 | −30,731.30 / −30,731.30 | −104.4988 / −104.4988 | −215.6415 / −215.6415 (−0.7353 / −0.7353) | 0.4008 / 0.4008 | 26.76% / 26.76% | 4.93% / 30.23% | 33,521.60 / 33,521.60 (110.9690 / 110.9690) |
| oco_bracket | 181 | 9,115.19 / 9,115.19 | 38.5346 / 38.5346 | 47.4943 / 47.4943 (0.2095 / 0.2095) | 1.2681 / 1.2681 | 42.54% / 42.54% | 13.26% / 9.79% | 4,584.43 / 4,584.43 (12.0145 / 12.0145) |

| Mode | Execution / financing cost | Break-even pips/completed side | Actual sides / weighted equivalents | Entry / exit fills | Median / p95 hold | Max concurrency | Suppressed | Unresolved | PropGuard |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | 0 / 0 | −8.3659 | 764 / 764 | 384 / 380 | 22h / 63h | 3 | 70 | 2 | clear, 0 breaches |
| synthetic_breakout | 0 / 0 | −20.5920 | 289 / 289 | 145 / 144 | 11.5h / 59h | 3 | 116 | 2 | clear, 0 breaches |
| contingent_hedge | 0 / 0 | −54.4859 | 563 / 563 | 282 / 281 | 14h / 91h | 3 | 118 | 2 | clear, 0 breaches |
| oco_bracket | 0 / 0 | 23.7471 | 364 / 364 | 183 / 181 | 25h / 63h | 3 | 70 | 2 | clear, 0 breaches |

**Hedge minus synthetic attribution.** Gross difference was −317.40 pips / +0.8934R. Explicitly
tagged gap structures contributed −9,728.20 pips / −32.6576R (32 hedge structures, four synthetic),
and non-overlapping same-bar tags contributed +5,613.70 pips / +20.6862R (56 hedge, 41 synthetic).
The residual gross-payoff bucket was +3,797.10 pips / +12.8647R. Execution and financing cost
differences were both zero in this configured-zero-cost cell, so net difference remained −317.40
pips / +0.8934R. Reconciliation error was 0.0 pips and approximately `3.6e-15`R. Actual fill
decomposition was hedge 384 entries + 380 exits versus synthetic 145 entries + 144 exits.

This four-month local cache is implementation and descriptive comparison evidence, not an
out-of-sample strategy-selection result. In particular, the positive bracket row does not
authorize Phase 3 tuning or deployment. The hedge did **not** repay its extra transaction sides:
it used 764 actual sides versus synthetic's 289 and finished 317.40 net pips behind even before
charging those extra sides; any positive per-side execution cost would widen that deficit in this
cell.

## Phase 2 closeout and S8 handoff

**Delivered baseline.** Phase 2 is closed by five ordered commits:
`65476b0` (`hedge_pair` golden parity), `e54ee01` (`synthetic_breakout`), `97259a2`
(`contingent_hedge`), `9fbad4f` (`oco_bracket`), and `7fdb676` (four-mode comparison). The final
gate collected 165 Python tests: 161 passed and four remained explicitly skipped for absent
historical export fixtures. Ruff, Python compilation, 15 frontend tests, the production build,
`git diff --check`, and the local H1 end-to-end comparison passed. No Phase 3 parameter or research
study was implemented.

**Next-goal input audit.** The currently available local M15 cache contains 2,000 bars from
2026-07-21 05:45 UTC through 2026-08-19 23:30 UTC with candle fingerprint
`85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900`. This is sufficient to
exercise and verify the S8 harness, but it is only about 30 days and is not sufficient for a
strategy-selection claim. The named longer M15/H1 exports and a local covering M1 cache remain
absent. S8 must therefore publish the complete 256-cell surface, label the conservative no-subpath
resolver fallback, retain the export-dependent skips, and treat all measured rankings as local
descriptive evidence only.

**Gross/net delta.** This handoff changes documentation only: 0.0 gross pips / 0.0 gross R and
0.0 net pips / 0.0 net R. It does not add, rerun, select, or tune any strategy cell.

## S8 256-cell scale decomposition

**Command.** One local run, no network, no fitting:

```
session-hedging --run-s8-scale-sweep --symbol XAUUSD
```

It wrote `reports/research/s8-scale-decomposition.json` and
`reports/research/s8-scale-decomposition.md`, both committed in full.

**Input.** `data/candles/XAUUSD/M15.jsonl`: 2,000 M15 bars from 2026-07-21 05:45 UTC through
2026-08-19 23:30 UTC, candle fingerprint
`85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900`. That is the same fingerprint
recorded in the Phase 2 closeout audit above, and one fingerprint is shared by all 256 cells.

**Grid.** `4 entry modes x ORB_MINUTES {15,30,60,120} x ENTRY_DELAY_MINUTES {0,15,30,60} x
MAX_AGE_HOURS {8,12,24,48} = 256` cells, all with `TIME_EXIT_MODE=max_age`. Every cell was built
through `EngineParams` validation, and a runtime guard rejects any cell differing from the shared
configuration outside those four fields. Sessions (`tokyo,london,new_york`), `STOP_MODE=bar_range`,
`SL_MULT=2`, `RR=3`, `LOCK_MODE=absolute` at 20 pips, `ANCHOR_TOLERANCE_MINUTES=15`,
`INTRABAR_MODE=m1_conservative`, `RISK_MODE=fixed_qty` at `QTY=1`, a three-structure cap,
`ONE_OPEN_PER_SESSION=true`, `FIRM_PROFILE=none`, and configured-zero execution and financing
rates were identical in every cell.

**M1 coverage: absent.** No `data/candles/XAUUSD/M1.jsonl` exists, so 0 of 2,000 parent bars had a
covering M1 bar and no M1 chronology was used anywhere in this run. Every cell resolved ambiguous
bars through the resolver's documented conservative no-subpath fallback,
`pessimistic_same_bar_no_subpath`: when one bar touches both the stop and the target, the stop is
taken first. The artifacts carry this state in `m1_coverage` and in the rendered header.

**Gross/net evidence.** Costs are configured at zero in this cell, so gross and net are equal
everywhere in this run; they are reported as a pair regardless, because §0.7 showed they can
disagree in sign once costs are non-zero. Execution and financing cost were 0.0 pips in all 256
cells. Across the surface: 12,373 completed structures (20 to 65 per cell), 0 unbucketed, 0
PropGuard breaches, unresolved structures 0 to 2 per cell, max concurrency 2 to 3, suppressed
signals 0 to 43, transaction sides 42 to 260.

| Quantity | Range across the 256 cells |
|---|---|
| Gross / net equity pips | −7,208.40 to +3,808.90 (identical gross and net) |
| Gross / net equity R | −46.1556 to +14.8187 |
| Net expectancy R per completed structure | −0.7783 to +0.3024 |
| Net profit factor | 0.3481 to 1.9075 |
| Survivor TP rate | 0.00% to 35.42% |
| Break-even TP rate required | 0.45% to 37.88% |
| TP-rate margin | −25.00pp to +12.00pp |
| TP-rate margin CI lower bound | −30.89pp to +1.68pp |
| Net maximum drawdown R | 3.0469 to 46.3658 |
| Median hold | 2.0h to 48.25h (p95 8.25h to 91.81h) |
| Break-even pips per completed side | −42.6631 to +38.8663 |

Summed over all 256 cells the surface is negative: −68,865.20 gross pips and −68,865.20 net pips,
−704.7176 gross R and −704.7176 net R on completed structures. 120 of 256 cells finished above
zero net R. Only 12 of 256 cells have a TP-rate margin confidence interval excluding zero, so on
the §9 gate "does the TP rate clear its bar" 244 of 256 cells are not distinguishable from zero.

**Per-mode marginals (descriptive, not a ranking to act on).**

| Mode | Cells | Completed | Median net R | Mean net R | Min / max net R | Cells net R > 0 |
|---|---:|---:|---:|---:|---:|---:|
| hedge_pair | 64 | 3,582 | 1.3598 | 0.5048 | −13.1962 / 12.9569 | 36 |
| synthetic_breakout | 64 | 2,931 | 0.2380 | 0.0268 | −9.4046 / 14.8187 | 32 |
| contingent_hedge | 64 | 2,824 | −8.8437 | −14.2265 | −46.1556 / 4.7816 | 12 |
| oco_bracket | 64 | 3,036 | 1.8310 | 3.1430 | −15.7182 / 14.4274 | 40 |

**Hold-bucket attribution, summed over the whole surface.** Buckets are fixed, non-overlapping and
exhaustive; bucket counts plus unbucketed equal the completed count in every cell, and 0 structures
were unbucketed.

| Bucket | Structures | Gross R | Net R | Gross pips | Net pips |
|---|---:|---:|---:|---:|---:|
| [0h,8h] | 5,714 | −1,713.8655 | −1,713.8655 | −400,843.70 | −400,843.70 |
| (8h,12h] | 2,370 | +264.1276 | +264.1276 | +68,533.83 | +68,533.83 |
| (12h,24h] | 1,965 | +497.6400 | +497.6400 | +177,642.35 | +177,642.35 |
| (24h,48h] | 1,193 | +173.0960 | +173.0960 | +54,323.27 | +54,323.27 |
| (48h,+inf) | 1,131 | +74.2843 | +74.2843 | +296.73 | +296.73 |

On this window the short-hold bucket carries the entire loss and every longer bucket is positive in
both pips and R. That is consistent with the §9 holding-horizon prior, and it is one month of one
symbol; it is not confirmation.

**Structural degeneracy found by the run.** Entry time is
`max(anchor + ORB_MINUTES, anchor + ENTRY_DELAY_MINUTES)`, so any delay at or below the opening
range is absorbed by the range close. The 256 cells therefore contain only **112** distinct
effective configurations: 144 cells are duplicates by construction. All 64 collapsed groups agree
exactly on gross R, net R and completed count, which is the expected result and is now asserted by
a test. The `ENTRY_DELAY_MINUTES` axis in §8.1 does not carry four independent levels against these
ORB values, and the near-identical delay rows in the marginal table must be read that way rather
than as four measurements.

**Determinism.** Two consecutive runs on the same cache produced byte-identical JSON and Markdown
(`c486cec61215447e5892cee933d10e7eb29b50079e6242fae59a7462518c507a` and
`e651b51018aa092305c0a0373e66eb08933a6e792175c1016bdba4ba246f6952`).

**Caveats.** This is a 30-day, 2,000-bar local cache on one symbol. It is sufficient to verify the
harness and to describe behaviour, and it is not sufficient to select a configuration: no
walk-forward, deflated Sharpe, PBO or Monte Carlo has been run, costs are configured rather than
measured from broker ticks, and no covering M1 data was available. No cell was chosen, no parameter
was tuned, no losing cell was removed, and the negative aggregate stands as reported. The §8.1
hypothesis (that `ORB_MINUTES=60` recovers the H1 result) is **not** settled by this run: the
`orb_minutes=30` column has the highest marginal median net R here, but on 112 distinct
configurations over one month that is an artefact until it survives out of sample.

**Gross/net delta.** S8 adds measurement only. The Phase 0–2 execution path is unchanged and the
four-mode comparison output is identical, so the production-path delta is 0.0 gross pips / 0.0
gross R and 0.0 net pips / 0.0 net R. The 190-test Python suite passes with the same four
export-dependent skips as the Phase 2 closeout, which remain unverified.

## S1–S4 and S9 research studies

**Commands.** Five local runs, no network, no fitting:

```
session-hedging --run-s1-target-hit
session-hedging --run-s2-break-frequency
session-hedging --run-s3-anchor-study
session-hedging --run-s4-cost-sensitivity
session-hedging --run-s9-regime-attribution
```

Each writes `reports/research/<study>.json` and a rendered `.md`, both committed in full.

**Input.** The same 2,000-bar local M15 cache as S8: 2026-07-21 05:45 UTC to 2026-08-19 23:30 UTC,
fingerprint `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900`, sessions
`tokyo,london,new_york`, `ORB_MINUTES=60`, `ENTRY_DELAY_MINUTES=15`, `TIME_EXIT_MODE=max_age` at
24 hours, `SL_MULT=2`, `RR=3`, 20-pip absolute lock, `RISK_MODE=fixed_qty`, three-structure cap,
one open per session, `FIRM_PROFILE=none`, and configured-zero execution and financing rates
except where S4 sweeps them. The reference run produces 65 signals and 49 structures.

**Definitions recovered, not invented.** The v3 specification defers S1–S7 to "v2 §8", and the v2
document was deleted in commit `522e285`. The definitions used here were recovered from that
commit (`git show 522e285^:session-hedging/session-hedging-improvement-spec-v2.md`) rather than
reconstructed from the v3 summary lines, so S1's conditional formulation, S2's grouping, S3's
anchor grid and S4's 2x headroom gate are the author's, not mine.

**M1 coverage: partial, and deliberately unused.** A 2,000-bar M1 cache appeared during this work
covering 2026-08-18 23:22 to 2026-08-20 09:44, which is 93 of the 2,000 M15 parent bars, or 4.65%
of the window. Feeding it to the engine would resolve the last day and a half on M1 chronology and
the rest on the fallback, making cells inside one study incomparable. All five studies therefore
use the conservative `pessimistic_same_bar_no_subpath` fallback across the whole window and report
`m1_coverage.status = partial` with `subpath_used = false`. **S8 was re-run for this reason and its
committed artifact refreshed: all 256 cells are byte-identical to the original run, and only the
M1-coverage block changed from `absent` to `partial`.** No S8 number moved.

### S1 conditional target-hit

49 structures produced 36 conditioned survivors: 12 were excluded because both legs stopped
together and one because no leg stopped. The lock (20 pips) was touched by 24 of the 36 and
collapsed to entry in none. ATR tercile edges were 61.87 and 76.64 pips.

| Horizon | k=1 | k=1.5 | k=2 | k=2.5 | k=3 | k=4 |
|---|---:|---:|---:|---:|---:|---:|
| 8h unconditional | 94.4% | 61.1% | 36.1% | 27.8% | 13.9% | 5.6% |
| 24h unconditional | 94.4% | 75.0% | 52.8% | 36.1% | 30.6% | 13.9% |
| 24h lock-survived | 94.4% | 66.7% | 47.2% | 30.6% | 25.0% | 11.1% |
| 48h unconditional | 100.0% | 80.6% | 61.1% | 50.0% | 36.1% | 19.4% |
| 48h lock-survived | 94.4% | 66.7% | 50.0% | 38.9% | 27.8% | 13.9% |

All on n=36. The 24-hour interval for `k=3` is 18.0% to 46.9%, which spans both sides of the
32%-ish break-even TP rate the §0.5 arithmetic demands, so this sample cannot say whether `RR=3`
clears its bar. Median MFE at 24 hours was 692.0 pips (p95 1,744.7) against a median MAE of 0.0
(p95 1,056.0); in opening-range units the median survivor travelled 4.01 ranges favourably.
Reach frequencies are upper bounds: a bar's extreme is credited to the whole bar, and the stop bar
itself is excluded from the walk.

### S2 single-break versus double-break

65 episodes, none without forward bars. Classes are exhaustive at every horizon.

| Horizon | No break | Single break | Double break | Ambiguous same bar | Single rate (95% CI) | Median first break | Median opposite break |
|---|---:|---:|---:|---:|---:|---:|---:|
| 4h | 1 | 44 | 19 | 1 | 67.7% (55.6–77.8%) | 0.25h | 0.75h |
| 8h | 1 | 34 | 29 | 1 | 52.3% (40.4–64.0%) | 0.25h | 1.38h |
| 12h | 1 | 30 | 33 | 1 | 46.2% (34.6–58.2%) | 0.25h | 2.25h |
| 24h | 1 | 27 | 36 | 1 | 41.5% (30.4–53.7%) | 0.25h | 2.50h |
| 48h | 1 | 20 | 43 | 1 | 30.8% (20.9–42.8%) | 0.25h | 4.88h |

The first side breaks within 15 minutes in the median episode, and by 24 hours the opposite side
has been tested in 56.9% of them. By session at 24 hours the single-break rate was Tokyo 47.6%,
London 40.9%, New York 36.4%. Engine companions on the same window: `hedge_pair` 1 whipsaw in 48
completed (2.1%), `contingent_hedge` 8 in 34 (23.5%), `synthetic_breakout` and `oco_bracket` none.
Loss-closed shares of triggered structures were 49.0%, 42.9%, 57.9% and 51.2% respectively.

### S3 anchor study

Nine anchor variants, each run as the only session. No variant produced an anchor-drift skip.

| Anchor | Signals | Completed | Net R | Net expectancy R | Survivor TP | Required TP | Margin pp (CI low) | Median ORB pips | Range expansion | Volume expansion |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| tokyo 09:00 (incumbent) | 21 | 14 | +1.5103 | +0.1079 | 14.3% | 9.4% | +4.89 (−5.39) | 205.8 | 1.758 | 1.648 |
| tokyo 08:45 | 21 | 13 | +2.8005 | +0.2154 | 15.4% | 5.2% | +10.21 (−0.84) | 195.2 | 2.068 | 1.693 |
| london 08:00 (incumbent) | 22 | 19 | −4.3803 | −0.2305 | 15.8% | 24.5% | −8.70 (−18.97) | 127.5 | 0.743 | 0.904 |
| london 10:30 | 22 | 21 | −8.1370 | −0.3875 | 14.3% | 28.2% | −13.91 (−23.22) | 96.2 | 0.934 | 0.962 |
| london 15:00 | 22 | 12 | +4.2898 | +0.3575 | 8.3% | 9.4% | −1.09 (−7.94) | 218.0 | 0.765 | 1.007 |
| new_york 08:00 (incumbent) | 22 | 15 | +4.1611 | +0.2774 | 20.0% | 7.1% | +12.88 (−0.07) | 211.5 | 1.542 | 1.240 |
| new_york 08:20 | 22 | 15 | +4.6277 | +0.3085 | 13.3% | 2.4% | +10.98 (+1.38) | 190.5 | 1.281 | 1.215 |
| new_york 08:30 | 22 | 15 | +4.6277 | +0.3085 | 13.3% | 2.4% | +10.98 (+1.38) | 190.5 | 1.281 | 1.215 |
| new_york 09:30 | 22 | 12 | +2.0244 | +0.1687 | 0.0% | 7.8% | −7.78 (−7.78) | 262.2 | 1.315 | 1.177 |

Two things stand out and neither is a recommendation. **New York is the healthiest session on this
window, not the sick one**: all four New York anchors finished positive, and 08:20/08:30 are the
only rows anywhere whose TP-rate margin interval excludes zero. **London is the problem here**,
with the incumbent and the AM auction both negative and both showing range expansion **below 1.0**
— the London opening range is narrower than the hour before it, so that anchor is not marking an
expansion event at all on this data. This inverts the pilot's prior, on one month, and is exactly
the kind of claim §9 requires walk-forward evidence for.

**Bar-resolution degeneracy.** New York 08:20 and 08:30 produced identical results because an
anchor off a bar boundary snaps forward to the next M15 open, so both describe the same four bars.
That is one observation, not two agreeing anchors, and the report says so.

### S4 cost sensitivity

144 cells: four modes x spread {0,1,2,3,4,6} x slippage {0,0.5,1} x commission {0,0.5} pips per
side. Gross is identical down each cost ladder, as it must be, and net falls monotonically.

| Mode | Net R at zero cost | Break-even pips per completed side | Highest cost/side still net-pips positive | Cells meeting 2x | Best headroom |
|---|---:|---:|---:|---:|---:|
| hedge_pair | +1.2911 | 3.790 | 3.5 | 6 / 36 | 3.79x |
| synthetic_breakout | +1.0187 | 7.454 | 7.5 | 18 / 36 | 7.45x |
| contingent_hedge | +0.8555 | 7.667 | 7.5 | 18 / 36 | 7.67x |
| oco_bracket | +6.2044 | 16.925 | 7.5 | 30 / 36 | 16.93x |

Against a realistic 2 to 4 pips per side, `hedge_pair` has roughly 1.0x to 1.9x headroom and fails
the §9 2x gate at any cost above 1.9 pips; the two single-entry modes sit near 1.9x to 3.7x, and
the bracket is the only mode with a comfortable margin on this window. That ordering follows
directly from transaction sides, which is the same argument §0.6 made against the hedge.

**Net pips and net R disagree in sign in 45 of the 144 cells.** Pips weight every structure
equally while R divides each by its own stop, so a handful of wide-stop winners can carry a
positive pip total while the R total is negative. Both columns are now reported per cell; neither
is the answer alone, and this is §0.7's warning reproduced on live measurement rather than quoted.

### S9 regime and trend attribution

Over this window gold rose from 4,066.03 to 4,517.73, +4,517.0 pips, across 13 `up` days, 4 `flat`,
4 `down` and 5 `warmup` (trailing five-day slope, ±50 pips/day deadband).

| Mode | Completed | Net R | TP | Long winners | Short winners | Long share (95% CI) | Net R up-regime | Net R down-regime |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | 48 | +1.2911 | 8 | 6 | 2 | 75.0% (40.9–92.9%) | +2.8038 | +0.6493 |
| synthetic_breakout | 37 | +0.8969 | 7 | 6 | 1 | 85.7% (48.7–97.4%) | +2.8949 | −0.3639 |
| contingent_hedge | 34 | +0.7336 | 4 | 4 | 0 | 100.0% (51.0–100.0%) | −3.8833 | +3.0630 |
| oco_bracket | 40 | +5.7840 | 4 | 2 | 2 | 50.0% (23.7–76.3%) | +4.3012 | +2.6895 |

Four flags fired: directional winner concentration on `hedge_pair`, `synthetic_breakout` and
`contingent_hedge`, and calendar-half concentration on `oco_bracket`, whose second half carries
73.9% of its absolute net R. Three of four modes take at least three quarters of their surviving
winners from the long side in a month when gold rose 11%. That is the §12 trend confound, measured
rather than suspected. The intervals are wide enough to include an even split in every case, so
the flags mark concentration, they do not establish it — and with four `down` days this window
cannot test the other regime at all.

**Gross/net delta.** These five studies add measurement only. The Phase 0–2 execution path is
unchanged, the four-mode comparison output is identical, and S8's 256 cells are byte-identical
after its re-run, so the production-path delta is 0.0 gross pips / 0.0 gross R and 0.0 net pips /
0.0 net R. Costs are configured at zero outside S4's sweep, so gross equals net in S1, S2, S3 and
S9; both are reported as a pair regardless. The Python suite is 245 passed and 4 skipped, the same
four export-dependent skips, which remain unverified.

**What none of this establishes.** One month, one symbol, one cache. No walk-forward, no deflated
Sharpe, no PBO, no Monte Carlo. No anchor was moved, no `RR` chosen, no mode selected, no cost
budget adopted, and no parameter tuned in response to any number above. Phase 3 remains gated on
§9.

## Reporting unit moved to the client

**Change.** Pips versus dollars is no longer an environment setting. `PERFORMANCE_UNIT` and
`DOLLARS_PER_PIP_PER_QTY` are removed from `.env` and `.env.example` and from `Settings`
altogether. The UI selects the unit per run and, when dollars are selected, supplies the
dollar-per-pip rate at `QTY=1`, defaulting to **10**; both travel on the backtest request.
`/v1/config` publishes `default_dollars_per_pip_per_qty` so the client does not hard-code it.

**What "all metrics in the selected unit" means here.** The engine still computes in
`pips_weighted` — pips are the invariant the price data supports — and the report now carries one
`performance` block that restates every additive metric in the selected unit: realized, unrealized
and equity (gross, cost and net), execution and financing cost, maximum drawdown gross and net,
break-even cost per completed side, and the configured per-side costs. The block also states
`unit`, `dollars_per_pip_per_qty` and the single `conversion_factor` applied, so any figure in it
can be checked by hand. Comparison rows carry the same view, and the hedge-minus-synthetic
reconciliation ledger is restated by the same factor, which leaves its identity intact.

**R is never converted.** R is a ratio; §2's rule that a pip total must always appear beside its R
total now reads as "the unit amount always appears beside its R total". The pip-denominated fields
stay on the report unchanged in either mode, so the raw series and the CSV export remain comparable
across runs.

**Verified conversion, one local run** on the 2,000-bar M15 cache, `hedge_pair`, sessions
`tokyo,london,new_york`, configured-zero costs:

| Reporting unit | Conversion factor | Net equity | Gross max drawdown | Break-even per completed side | Gross R |
|---|---:|---:|---:|---:|---:|
| pips | 1.0 | +727.70 pips | 2,246.60 pips | 3.7901 pips | +1.2911R |
| dollars, rate 10 (default) | 10.0 | +$7,277.00 | $22,466.00 | $37.90 | +1.2911R |
| dollars, rate 2.5 | 2.5 | +$1,819.25 | $5,616.50 | $9.4753 | +1.2911R |

The same three runs were exercised through the UI: the KPI strip, the session rail and the
comparison table all render in the selected unit, the dollar-per-pip field appears only when
dollars are selected, and R is unchanged in every tile.

**Sizing keeps its cash rate.** Fixed-fractional sizing and `FIRM_PROFILE=custom` size in account
currency regardless of how results are displayed, so they receive the same rate: the client's when
a request supplies one, otherwise the default 10. Two former startup errors
(`RISK_MODE=fixed_fractional` and `FIRM_PROFILE=custom` without a configured rate) are therefore
now defaulted rather than rejected, and their tests were rewritten to assert the new contract
rather than deleted.

**Phase 1 parity is intact.** The W2.1 golden fixture still hashes bit-for-bit. The `performance`
block is excluded from that payload exactly as `entry_mode`, `pending_entry_orders` and
`unresolved_structures` already were: it did not exist in the captured Phase 1 report and it
restates existing numbers rather than changing any. Phase 5 later applies the same explicit rule
to `report_header` and six per-structure diagnostic fields.

**Not converted, deliberately.** The six research artifacts under `reports/research/` remain pip-
and R-denominated. They are offline evidence produced by CLI commands with no client and no unit
selection, and §2 makes R the sign-of-truth for exactly the cross-cell comparisons those studies
make. Converting them would add a presentation unit to files whose purpose is comparability.

**Gross/net delta.** This change is presentation only: 0.0 gross pips / 0.0 gross R and 0.0 net
pips / 0.0 net R. No trading behaviour, no parameter, and no research result moved. The Python
suite is 253 passed and 4 skipped; the frontend suite is 18 tests.

## §9 Phase 3 gate scorecard

**Command.** `session-hedging --run-phase3-gate-scorecard` read the six already-committed S1, S2,
S3, S4, S8 and S9 JSON surfaces and wrote
`reports/research/phase3-gate-scorecard.{json,md}`. Every source shares the 2,000-bar M15 candle
fingerprint `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` and date bounds
2026-07-21 05:45 UTC through 2026-08-19 23:30 UTC. The command evaluates the pre-written gates; it
does not tune, re-window, select an argmax, or discard a losing cell.

**Decision: 1 of 10 gates passes.** Anchor drift passes. TP-rate margin, hedge versus synthetic,
and cost headroom fail. Scale, RR, lock, holding horizon, edge reality and prop survivability are
not yet testable. In particular, only 12 of the unfrozen 256 S8 cells have a TP-rate-margin lower
confidence bound above zero; the hedge has 1.90x headroom at 2 modeled pips per side and 0.95x at
4, below the required 2x; and no unseen folds, deflated Sharpe ratio, PBO or broker-measured cost
interval exists. The TP-rate margin and cost-headroom gates therefore fail and edge reality does
not pass. **Phase 3 redesign is NOT authorized.** S6 and S7 must run against the incumbent four
modes.

**M1 and data sufficiency.** M1 coverage is partial: 93 of 2,000 parent bars (4.65%). No M1
chronology is mixed into this window; every source uses the conservative
`pessimistic_same_bar_no_subpath` fallback. Roughly 30 days of one symbol is sufficient to verify
the scorecard harness and describe behavior, and is not sufficient for walk-forward selection or
a prop-survivability claim. Those claims need multiple years of contiguous M15 with covering M1
and broker bid/ask, slippage, commission, swap, margin and gap observations spanning varied
regimes.

**Gross/net delta.** The scorecard adds measurement only: 0.0 gross pips / 0.0 gross R and 0.0 net
pips / 0.0 net R. The production path and all six source artifacts are unchanged.

## S5 resolver-ladder bias calibration

**Command.** One controlled local run, no fitting:

```text
session-hedging --run-s5-resolver-bias \
  --date-from 2026-07-21T05:45:00+00:00 \
  --date-to 2026-08-19T23:30:00+00:00
```

It wrote `reports/research/s5-resolver-bias.{json,md}` from the same 2,000-bar M15 fingerprint
`85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` used by the six earlier
research surfaces. Every tier shares one configuration; only `INTRABAR_MODE` changes. Tier 4 is
reported as interface-only because no bid/ask tick source is implemented.

| Tier | Resolver | Same-bar rate / R | Gross / net pips | Gross / net R | Delta vs tier 0 | Changed |
|---:|---|---:|---:|---:|---:|---:|
| 0 | optimistic | 22.92% / −1.6935R | +657.90 / +657.90 | +0.8680 / +0.8680 | 0.00 pips / 0.0000R | 0 |
| 1 | pessimistic | 25.00% / −2.5723R | +727.70 / +727.70 | +1.2911 / +1.2911 | +69.80 pips / +0.4230R | 1 |
| 2 | M1 | 25.00% / −2.5723R | +727.70 / +727.70 | +1.2911 / +1.2911 | +69.80 pips / +0.4230R | 1 |
| 3 | M1 conservative | 25.00% / −2.5723R | +727.70 / +727.70 | +1.2911 / +1.2911 | +69.80 pips / +0.4230R | 1 |
| 4 | tick | unavailable | unavailable | unavailable | unavailable | unavailable |

**Why the local ladder is inverted.** Only `london:2026-08-18T08:00:00+00:00` changes. Tier 0
allows the structure to finish as a −214.80-pip / −1.3018R whipsaw. Tiers 1–3 recheck the newly
locked survivor stop on the same bar and finish it as a −145.00-pip / −0.8788R lock. The adverse-
first rule is therefore +69.80 gross/net pips and +0.4230 gross/net R better on this window. This
is a traceable multi-leg state transition, not a hidden configuration change, and it is published
even though it does not reproduce the pilot prior.

**M1 and export limits.** M1 covers 93 of 2,000 parent bars (4.65%). Partial chronology is not
mixed: tiers 2 and 3 use `pessimistic_same_bar_no_subpath` across the whole window and therefore
equal tier 1. The M15/H1/H4 export CSVs remain absent, the fixture-dependent S5 test is an explicit
skip, and the §0 10.6% / 11.2% / 5.1% comparison remains **unverified**. No fixture data was
invented, synthesized or approximated. Roughly 30 days of one symbol verifies the harness and is
descriptive only; proper calibration needs the named exports plus contiguous M15 with covering M1
or bid/ask ticks across varied regimes.

**Gross/net delta.** S5 adds measurement only. The Phase 0–2 production path changes by 0.0 gross
pips / 0.0 gross R and 0.0 net pips / 0.0 net R.

## S6 nested walk-forward

**Protocol freeze and command.** Commit `67e98e7` froze the candidate set, 800/200 rolling
windows, 400-bar final holdout, selection rule, DSR formula and eight-block CSCV protocol in the
specification before S6 accessed the holdout. The subsequent controlled run was:

```text
session-hedging --run-s6-walk-forward \
  --date-from 2026-07-21T05:45:00+00:00 \
  --date-to 2026-08-19T23:30:00+00:00
```

The fingerprint is `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900`.
All four incumbent modes were evaluated on every training slice. Session anchors,
`ENTRY_MODE`, `ORB_MINUTES`, `ENTRY_DELAY_MINUTES`, `MAX_AGE_HOURS`, `SL_MULT`, `RR`,
`LOCK_MODE`, `LOCK_PIPS` and both hedge ratios are explicit in every coordinate. Since redesign
was not authorized, all axes except entry mode retain their incumbent singleton value. The JSON
logs 57 evaluations: 16 rolling training, four immediately following unseen tests, four final
pre-holdout training, one final unseen holdout, and 32 CSCV block/configuration cells. It also
publishes all 70 CSCV splits.

| Fold | Selected mode | Train net exp R | Unseen gross / net pips | Unseen gross / net R | Unseen net exp R |
|---:|---|---:|---:|---:|---:|
| 0 | contingent hedge | +0.1733 | −281.90 / −281.90 | −0.5084 / −0.5084 | +0.2430 |
| 1 | OCO bracket | +0.0860 | −739.84 / −739.84 | −2.3819 / −2.3819 | −1.0000 |
| 2 | contingent hedge | +0.1505 | +372.50 / +372.50 | +0.7330 / +0.7330 | +0.2735 |
| 3 | synthetic breakout | +0.1077 | −1,067.60 / −1,067.60 | −3.7137 / −3.7137 | −0.9284 |

**Unseen-only result.** The rolling aggregate contains only the four `unseen_test` evaluation IDs:
−1,716.84 gross pips / −1,716.84 net pips and −5.8710 gross R / −5.8710 net R across 11 completed
structures. Completed-structure expectancy is −148.67 gross/net pips and −0.5137 gross/net R.
Training and CSCV block values are excluded. Gross and net are equal because the controlled
configuration has zero execution and financing cost; both are retained explicitly.

**DSR, PBO and final holdout.** On the 11 selected unseen structure net-R observations, raw Sharpe
is −0.5170, expected maximum Sharpe over four trials is 1.0521, and the deflated Sharpe probability
is **0.0305%**. Eight-block CSCV yields **PBO 40.0%** over 70 splits. The final 400-bar holdout is
reported separately: the 1,600-bar pre-holdout selection chose `oco_bracket`, which finished the
holdout at +236.09 gross/net pips and +1.2354 gross/net R. That single positive slice does not
override the negative rolling aggregate and is not a production selection.

**M1 and sufficiency.** M1 coverage is partial at 93/2,000 parent bars (4.65%); no chronology is
mixed and the full protocol uses `pessimistic_same_bar_no_subpath`. Roughly 30 days verifies fold
boundaries, train-only selection, unseen-only aggregation, DSR and PBO. It cannot establish an
edge or select a mode. That requires multiple years of contiguous M15 with covering M1 and
measured broker spread, slippage, commission and swap across varied regimes.

**Gross/net production delta.** S6 is offline measurement only: 0.0 gross pips / 0.0 gross R and
0.0 net pips / 0.0 net R on the Phase 0–2 production path.

## S7 PropGuard complete-cluster Monte Carlo

**Command and seed.** One controlled run used fixed seed **20260820**:

```text
session-hedging --run-s7-prop-monte-carlo \
  --date-from 2026-07-21T05:45:00+00:00 \
  --date-to 2026-08-19T23:30:00+00:00
```

It ran 2,000 100-day paths for each incumbent mode on fingerprint
`85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900`. Reruns are byte-stable.
The bootstrap unit is a complete cluster, never an individual leg: connected overlapping holding
intervals remain together, then consecutive overlap components in the same stop-distance
volatility tercile remain one block. This retains London/New York overlap, concurrent exposure and
local volatility-regime runs. Empirical libraries are small: 12 hedge, 14 synthetic, 4 contingent
and 15 bracket clusters.

**Stress model.** Every resampled structure receives a capped lognormal spread draw (2-pip median,
0.35 log sigma, 8-pip cap) and capped exponential slippage draw (0.5-pip mean, 5-pip cap) per side.
Losing structures receive an extra capped exponential gap loss (0.25R mean, 2R cap) with
probability `max(empirical gap rate, 2%)`. Equity is normalized to 100%; 1R moves it by the
configured 0.1%. Concurrent exposure subtracts `MAX_PAIR_RISK_PCT=0.2%` per active structure from
the normalized free-margin proxy. That proxy is not broker margin and the artifact says so.

| Mode | 3% / 5% daily breach | 6% / 10% total breach | Time to breach | P / time to 10% target | Min free margin p01 / p50 | Gross R p05 / p50 / p95 | Net R p05 / p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| hedge pair | 0% / 0% | 0% / 0% | undefined | 0% / undefined | 96.84% / 98.71% | −10.8383 / +4.0540 / +21.1397 | −16.6111 / −1.4867 / +15.8309 |
| synthetic breakout | 0% / 0% | 0% / 0% | undefined | 0% / undefined | 96.02% / 98.54% | −22.2053 / +2.2309 / +29.3432 | −24.5649 / −0.1584 / +26.9800 |
| contingent hedge | 0% / 0% | 0% / 0% | undefined | 0% / undefined | 95.70% / 98.07% | −18.9176 / +2.3498 / +27.6073 | −25.7821 / −3.9764 / +21.3673 |
| OCO bracket | 0% / 0% | 0% / 0% | undefined | 0% / undefined | 97.62% / 99.03% | −0.5675 / +19.0655 / +38.1936 | −3.2596 / +16.4081 / +35.6442 |

No conditional expected breach day or target day exists because no path reaches those events.
Zero breaches do not clear the §9 prop-survivability gate: the configured risk is small, the
100-day target is unreachable in every simulated path, and 4–15 empirical clusters per mode cannot
represent prop-account tails. Multiple years of complete clusters plus covering M1 and broker
bid/ask, slippage, gap, swap, contract and margin observations are required.

**M1 and gross/net.** Coverage is 93/2,000 parent bars (4.65%). No partial chronology is mixed;
every baseline uses `pessimistic_same_bar_no_subpath`. The JSON/Markdown pair gross and net pips
and R for baseline and every simulated distribution. Tail costs make net materially worse than
gross even though the source backtests have zero configured cost.

**Gross/net production delta.** S7 is offline measurement only: 0.0 gross pips / 0.0 gross R and
0.0 net pips / 0.0 net R on the Phase 0–2 production path.

## Phase 5 non-fixture testing and reporting

**Scope.** Commit `9d156d5` completes only the v2 testing/reporting work that does not depend on
the absent export CSVs. Backtests now expose an auditable run header plus structure-level stop,
holding, weekday and gross/cost/net R fields. The client renders R and holding histograms, MAE/MFE,
concurrency, paired gross/net session and weekday tables, and honest PropGuard availability labels.

**Verification.** The supported 32-cell configuration product ran successfully. Deterministic
fill/trigger/OHLC and no-double-close properties passed. Python verification is 330 passed and five
explicit fixture skips; frontend verification is 21 passed and the production build passes. Ruff
and `git diff --check` pass. Full details and skip names are in
`reports/research/phase5-non-fixture-verification.md`.

**Boundary.** Phase 5 remains incomplete. The M15/H1/H4 known-figure regressions, W1.1 cost
acceptance, W1.2 H1 sizing acceptance and S5 cross-timeframe rates remain unverified. Phase 3 was
not authorized by the gate scorecard. No missing observation was inferred or synthesized.

**Gross/net delta.** Presentation and testing only: 0.0 gross pips / 0.0 gross R and 0.0 net pips /
0.0 net R on the Phase 0–2 production path.
