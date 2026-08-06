from __future__ import annotations

from typing import Literal

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
