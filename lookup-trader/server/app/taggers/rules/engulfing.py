"""Engulfing bars: a body that swallows the prior body and reverses its sign."""

from __future__ import annotations

from collections.abc import Sequence

from app.taggers.confidence import graded
from app.taggers.thresholds import (
    ENGULF_BODY_RATIO,
    ENGULF_MIN_PRIOR_BODY_PCT,
    MIN_RANGE_ATR,
)
from app.taggers.types import Bar, BarTag

LOOKBACK = 2


def _engulfing(bars: Sequence[Bar], atr: float, side: int) -> BarTag | None:
    if len(bars) < LOOKBACK:
        return None
    prior, anchor = bars[-2], bars[-1]

    if side == 1:
        if not (anchor.is_bull and prior.is_bear):
            return None
        if not (anchor.open <= prior.close and anchor.close >= prior.open):
            return None
        overshoot = (prior.close - anchor.open) + (anchor.close - prior.open)
        setup_id = "bull_engulfing"
    else:
        if not (anchor.is_bear and prior.is_bull):
            return None
        if not (anchor.open >= prior.close and anchor.close <= prior.open):
            return None
        overshoot = (anchor.open - prior.close) + (prior.open - anchor.close)
        setup_id = "bear_engulfing"

    # A doji prior is engulfed by anything. Without this guard the rule fires on
    # every bar that happens to follow an indecisive one, which is most of them.
    if prior.body_pct < ENGULF_MIN_PRIOR_BODY_PCT:
        return None
    if anchor.body < ENGULF_BODY_RATIO * prior.body:
        return None

    range_atr = anchor.range / atr
    if range_atr < MIN_RANGE_ATR:
        return None

    return BarTag(
        setup_id=setup_id,
        confidence=graded(
            anchor.body / (ENGULF_BODY_RATIO * prior.body),
            1.0 + overshoot / prior.body,
            range_atr / MIN_RANGE_ATR,
        ),
        side=side,
    )


def bull_engulfing(bars: Sequence[Bar], atr: float) -> BarTag | None:
    return _engulfing(bars, atr, 1)


def bear_engulfing(bars: Sequence[Bar], atr: float) -> BarTag | None:
    return _engulfing(bars, atr, -1)
