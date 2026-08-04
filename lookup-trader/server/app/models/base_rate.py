from __future__ import annotations

from pydantic import BaseModel
from app.models.recommendation import RecommendationOut


class BaseRateCell(BaseModel):
    """One target/stop pair scored over the matched bars."""

    target_atr: float
    stop_atr: float
    wins: int
    decided: int
    win_rate: float | None = None
    expectancy_r: float | None = None
    expectancy_r_net: float | None = None
    effective_n: float | None = None


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
    expectancy_r_net: float | None = None
    # Independent observations, roughly decided / horizon. Adjacent bars share
    # all but one of their forward bars, so the row count overstates the sample
    # by about the length of the horizon.
    effective_n: float | None = None
    level_used: str
    dimensions_used: list[str] = []
    median_mfe_atr: float | None = None
    median_mae_atr: float | None = None
    # Every target against the same stop and the same matched bars.
    target_grid: list[BaseRateCell] = []
    horizon: int
    target_atr: float | None = None
    stop_atr: float | None = None
    side: int | None = None
    scored_side: int | None = None
    scored_direction: str | None = None
    recommendation: RecommendationOut | None = None
    min_samples_required: int | None = None
    decided_available: int | None = None
