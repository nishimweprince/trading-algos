"""Every number the rules compare against.

Deliberately module constants rather than `settings` fields. Settings carry the
`LOOKUP_` env prefix, so a threshold there could be changed by an environment
variable and then baked into an *incremental* build — leaving rows on either side
of the change sharing one `bar_feature_version` while disagreeing about what a
pin bar is, with nothing able to detect it. Keeping them here makes retuning a
code change, which is what the store already asks for: "a changed threshold is a
rebuild rather than a migration".
"""

from __future__ import annotations

# Shared by every rule. A textbook pattern inside a dead range is a rounding
# artefact, and this is the cheapest filter against the store filling with noise.
MIN_RANGE_ATR = 0.5

# --- engulfing ---
# The anchor body must at least match the prior body.
ENGULF_BODY_RATIO = 1.0
# With inclusive engulfment bounds, any bar trivially engulfs a doji prior — and
# doji priors are common. Without this the rule fires on hundreds of nothing bars.
ENGULF_MIN_PRIOR_BODY_PCT = 0.10

# --- pin bar ---
# Range-relative rather than a wick-to-body ratio: the same quantities
# `_signal_candle_anatomy` already reports, and they degrade gracefully on a
# zero-body bar where a ratio would run to infinity.
PIN_WICK_PCT = 0.55
PIN_BODY_MAX_PCT = 0.35
PIN_OPP_WICK_MAX_PCT = 0.20
PIN_CLOSE_POS = 0.60

# --- inside bar break ---
# Break distance past the mother bar's extreme that scores a full ratio point.
INSIDE_BREAK_ATR = 0.25
# Contraction of the inside bar against the mother bar that scores a full point.
INSIDE_COMPRESSION_SPAN = 0.50

# --- chart patterns ---
# These moved here from `patterns.py` for the reason the module docstring gives:
# one home for every tunable, so retuning is a rebuild rather than a migration.

# Bars either side of a bar that it must be the extreme of to count as a swing.
# The most load-bearing number in the file: it decides which pivots exist, and
# every chart pattern is built out of pivots. It was a `settings` field, and so
# an environment variable could change what every chart tag means and have the
# result baked into an incremental build under an unchanged
# `bar_feature_version` — the precise failure this module exists to prevent.
SWING_LOOKBACK = 5

# How close two pivots must be, in ATR, to read as "the same level". Wide on
# purpose — a double bottom whose second low undercuts the first by half an ATR
# is still a double bottom, and the graded score records that it was the looser
# of the two rather than discarding it.
LEVEL_TOLERANCE_ATR = 1.0

# A pattern needs room. Two lows three bars apart are noise, not a formation.
MIN_PIVOT_SEPARATION = 5

# Shortest leg over longest, across the whole formation. A head and shoulders
# whose left half took four bars and whose right half took fifty is a trend leg
# that happened to turn twice, not a formation with two shoulders.
MIN_TIME_SYMMETRY = 0.35

# How many pivots may have formed since the formation's last one. A reversal
# carves a new pivot when it breaks and another when it pulls back to the
# broken level; at zero offset the pattern disappears exactly as it completes.
# Two is those two turns and no more — by the third the market has moved on, and
# allowing four put a double bottom on 11% of all bars.
MAX_PIVOT_OFFSET = 2

# Floor on how far back the anchor may sit from the formation's last pivot
# before the pattern stops being about the current bar. Scaled up by
# `STALE_SPAN_FRACTION` of the formation's own span, capped at the last value —
# a sixty-bar structure is still current long after a twelve-bar one is stale.
MAX_BARS_SINCE_FORMATION = 10
STALE_SPAN_FRACTION = 0.75
MAX_BARS_SINCE_FORMATION_CAP = 60

# A neckline is allowed to slope — most textbook ones do — so the two pivots it
# runs through get a looser bound than the shoulders, which are not. This is a
# guard only: neckline tilt is deliberately absent from the graded axes, because
# a sloping neckline is a normal head and shoulders, not a worse one.
NECKLINE_TILT_ATR = 2.0

# Fraction of the opening range the boundaries must close (or open) by.
MIN_RANGE_CHANGE = 0.35

# Range change small enough to read as neither converging nor diverging, which
# is what separates a rectangle or a channel from a triangle or a broadening.
STEADY_RANGE_CHANGE = 0.20

# A triangle side counts as flat if it moves less than this many ATR across the
# whole formation. Also the allowance for two boundaries counting as parallel.
FLAT_SIDE_ATR = 0.5

# How far the head must clear its shoulders before the middle peak is a head
# rather than a slightly higher high. Doubles as the ceiling on a triple's
# middle touch: a triple top is the same shape with no member clearing the rest.
MIN_HEAD_OVERSHOOT_ATR = 0.5

# --- flags and pennants ---
# A continuation pattern is a consolidation *after a pole*. Without the pole it
# is just a small range, so the pole is the load-bearing half of the test.
POLE_MIN_ATR = 3.0
POLE_MAX_BARS = 20
# How much of the pole the consolidation may retrace before it stops being a
# pause in the move and becomes a reversal of it.
POLE_MAX_RETRACE = 0.62
