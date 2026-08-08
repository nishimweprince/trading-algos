from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MetaShadowPredictionOut(BaseModel):
    artifact_version: str
    meta_feature_version: Literal[1, 2]
    probability: float = Field(ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    would_take: bool
    created_at: datetime


class MetaReplayPredictionOut(BaseModel):
    artifact_version: str
    role: Literal["active", "challenger"]
    meta_feature_version: Literal[1, 2]
    probability: float = Field(ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    would_take: bool
    target_take_rate: float | None = None


class MetaReplayIndicativeLevelsOut(BaseModel):
    basis: Literal["signal_close"]
    reference_price: float
    atr_at_signal: float = Field(gt=0)
    stop_price: float
    target_price: float
    final_levels_pending: Literal[True]


class MetaReplayInferenceOut(BaseModel):
    symbol: str
    timeframe: str
    signal_ts: datetime
    side: Literal[-1, 1]
    status: Literal["research_shadow"]
    orders_enabled: Literal[False]
    calendar_coverage_ok: bool
    predictions: list[MetaReplayPredictionOut]
    indicative_levels: MetaReplayIndicativeLevelsOut
    contract: dict[str, Any]


class MetaShadowEventOut(BaseModel):
    event_id: str
    symbol: str
    timeframe: str
    signal_ts: datetime
    side: Literal[-1, 1]
    primary_setup_id: str
    setup_ids: list[str]
    confidence: float
    state: Literal["awaiting_entry", "open", "resolved", "ineligible"]
    ineligible_reason: str | None = None
    forward_evaluation_eligible: bool
    calendar_coverage_ok: bool
    calendar_manifest_sha256: str | None = None
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    outcome: Literal["win", "loss", "timeout"] | None = None
    net_r_3: float | None = None
    net_r_5: float | None = None
    net_r_8: float | None = None
    predictions: list[MetaShadowPredictionOut] = Field(default_factory=list)


class MetaShadowPageOut(BaseModel):
    items: list[MetaShadowEventOut]
    offset: int
    limit: int
    total: int


class MetaModelStatusOut(BaseModel):
    status: str
    orders_enabled: Literal[False]
    active_shadow: dict[str, Any] | None
    ledger: dict[str, Any]
    capital_boundary: dict[str, Any] | None
    capital_publish: dict[str, Any] | None
    calendar_manifest: dict[str, Any] | None
