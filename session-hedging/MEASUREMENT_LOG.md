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
