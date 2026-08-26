"""Payoff-matched single-position breakout control."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SyntheticOrderPlan:
    reference_entry: float
    sl_dist: float
    upper_trigger: float
    lower_trigger: float
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float


def synthetic_order_plan(
    *, entry: float, sl_dist: float, rr: float, lock_dist: float, tp_r: float | None = None
) -> SyntheticOrderPlan:
    locked = lock_dist if 0 < lock_dist <= sl_dist else 0.0
    target_r = rr if tp_r is None else tp_r
    return SyntheticOrderPlan(
        reference_entry=entry,
        sl_dist=sl_dist,
        upper_trigger=entry + sl_dist,
        lower_trigger=entry - sl_dist,
        long_sl=entry + locked,
        long_tp=entry + sl_dist * target_r,
        short_sl=entry - locked,
        short_tp=entry - sl_dist * target_r,
    )
