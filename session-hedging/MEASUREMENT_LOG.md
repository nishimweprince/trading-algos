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
