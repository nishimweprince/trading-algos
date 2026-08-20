"""Incumbent two-leg entry, isolated without changing its arithmetic."""

from __future__ import annotations

from entry.base import EntryPlan


def hedge_pair_plan(*, entry: float, sl_dist: float, rr: float) -> EntryPlan:
    return EntryPlan(
        reference_entry=entry,
        sl_dist=sl_dist,
        long_entry=entry,
        short_entry=entry,
        long_sl=entry - sl_dist,
        long_tp=entry + sl_dist * rr,
        short_sl=entry + sl_dist,
        short_tp=entry - sl_dist * rr,
        long_open=True,
        short_open=True,
    )
