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
