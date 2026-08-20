"""Mode-neutral entry plan contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """Absolute fills and exits produced by an entry mode."""

    reference_entry: float
    sl_dist: float
    long_entry: float
    short_entry: float
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    long_open: bool
    short_open: bool
