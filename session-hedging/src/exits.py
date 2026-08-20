"""Time-based exit predicates."""

from __future__ import annotations

from datetime import datetime, timedelta

from models import TimeExitMode


def time_exit_due(
    *, entry_ts: datetime, bar_close_ts: datetime, mode: TimeExitMode, max_age_hours: float
) -> bool:
    if mode is TimeExitMode.NONE:
        return False
    return bar_close_ts > entry_ts + timedelta(hours=max_age_hours)
