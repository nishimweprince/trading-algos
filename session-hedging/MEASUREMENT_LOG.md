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
