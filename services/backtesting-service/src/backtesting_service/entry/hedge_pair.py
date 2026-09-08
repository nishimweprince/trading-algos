"""Incumbent two-leg entry, isolated without changing its arithmetic."""

from __future__ import annotations

from .base import EntryPlan


def failure_threshold(
    *, entry: float, sl_dist: float, is_long: bool, hedge_failure_k: float
) -> float:
    """Price where the primary leg is declared failed and the hedge stages.

    The trigger sits ``hedge_failure_k`` inside the stop distance from entry,
    on the adverse side of the primary leg.
    """
    offset = sl_dist - hedge_failure_k * sl_dist
    return entry + offset if is_long else entry - offset


def contingent_hedge_touched(
    *, threshold: float, bar_low: float, bar_high: float, long_primary: bool
) -> bool:
    """Whether the bar reached the failure threshold on the primary side."""
    return bar_low <= threshold if long_primary else bar_high >= threshold


def contingent_hedge_fill(*, bar_open: float, threshold: float, long_primary: bool) -> float:
    """Gap-aware hedge fill: the open when it gapped through, else the threshold."""
    if long_primary:
        return bar_open if bar_open <= threshold else threshold
    return bar_open if bar_open >= threshold else threshold


def hedge_pair_plan(
    *, entry: float, sl_dist: float, rr: float, tp_r: float | None = None
) -> EntryPlan:
    target_r = rr if tp_r is None else tp_r
    return EntryPlan(
        reference_entry=entry,
        sl_dist=sl_dist,
        long_entry=entry,
        short_entry=entry,
        long_sl=entry - sl_dist,
        long_tp=entry + sl_dist * target_r,
        short_sl=entry + sl_dist,
        short_tp=entry - sl_dist * target_r,
        long_open=True,
        short_open=True,
    )
