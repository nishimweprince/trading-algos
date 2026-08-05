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
