from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class OutcomeDirectionOut(BaseModel):
    direction: Literal["long", "short"]
    side: Literal[1, -1]
    p_win: float = Field(ge=0, le=1)
    p_loss: float = Field(ge=0, le=1)
    p_timeout: float = Field(ge=0, le=1)


class OutcomeInferenceOut(BaseModel):
    long: OutcomeDirectionOut
    short: OutcomeDirectionOut
    model_version: str
    artifact_version: str
    schema_sha256: str
    outcome_feature_version: str
    feature_version: str
    bar_feature_version: str
    status: Literal["pilot_shadow"]
    pilot: Literal[True]
    promoted: Literal[False]


class OutcomeUnavailableDetail(BaseModel):
    code: Literal["outcome_artifact_absent", "outcome_artifact_incompatible"]
    message: str
    retryable: Literal[False] = False


class OutcomeUnavailableOut(BaseModel):
    detail: OutcomeUnavailableDetail


class ShadowPredictionOut(BaseModel):
    artifact_version: str
    model_version: str
    symbol: str
    timeframe: str
    ts: datetime
    side: Literal[1, -1]
    direction: Literal["long", "short"]
    p_win: float
    p_loss: float
    p_timeout: float
    expected_gross_r: float
    expected_net_r: float
    observed_spread: float | None = None
    action_threshold_r: float
    would_trade: bool
    empirical_base_rate: Any = None
    tags: Any = None
    schema_sha256: str
    feature_version: str
    bar_feature_version: str
    training_source: Literal["histdata"]
    live_source: Literal["capital"]
    source_boundary: datetime
    created_at: datetime
    outcome: Literal["win", "loss", "timeout"] | None = None
    resolution_as_of_ts: datetime | None = None
    resolved_at: datetime | None = None
