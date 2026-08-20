"""Incumbent two-leg entry, isolated without changing its arithmetic."""

from __future__ import annotations

from entry.base import EntryPlan


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
