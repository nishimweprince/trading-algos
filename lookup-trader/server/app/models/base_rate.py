from __future__ import annotations

from pydantic import BaseModel


class BaseRateOut(BaseModel):
    matched_count: int
    wins: int
    losses: int = 0
    decided: int
    timeouts: int = 0
    win_rate: float | None = None
    # Bounds are computed at `effective_n`, not `decided` — see below.
    wilson_low: float | None = None
    wilson_high: float | None = None
    expectancy_r: float | None = None
    # Independent observations, roughly decided / horizon. Adjacent bars share
    # all but one of their forward bars, so the row count overstates the sample
    # by about the length of the horizon.
    effective_n: float | None = None
    level_used: str
    dimensions_used: list[str] = []
    median_mfe_atr: float | None = None
    median_mae_atr: float | None = None
    horizon: int
    target_atr: float | None = None
    stop_atr: float | None = None
    side: int | None = None
    min_samples_required: int | None = None
    decided_available: int | None = None
