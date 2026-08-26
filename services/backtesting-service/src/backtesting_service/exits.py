"""Time-based exit predicates and frozen partial-trail geometry."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import TargetMode, TimeExitMode

PARTIAL_FRACTION = 0.5
PARTIAL_TP_R = 1.0


def time_exit_due(
    *, entry_ts: datetime, bar_close_ts: datetime, mode: TimeExitMode, max_age_hours: float
) -> bool:
    if mode is TimeExitMode.NONE:
        return False
    return bar_close_ts > entry_ts + timedelta(hours=max_age_hours)


def initial_target_r(
    *, tp_mode: TargetMode, rr: float, partial_tp_r: float = PARTIAL_TP_R
) -> float:
    """First take-profit R. Partial-trail starts at 1R; incumbent fixed_r uses RR."""
    if tp_mode is TargetMode.PARTIAL_TRAIL:
        return partial_tp_r
    return rr


def target_price(*, entry: float, sl_dist: float, target_r: float, is_long: bool) -> float:
    return entry + sl_dist * target_r if is_long else entry - sl_dist * target_r
