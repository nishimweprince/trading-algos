from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CandleOut(BaseModel):
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SetupOut(BaseModel):
    setup_id: str
    name: str
    description: str | None = None
    default_side: int | None = None
    category: str | None = None
    active: bool = True


class SessionCreate(BaseModel):
    symbol: str
    timeframe: str
    date_from: datetime
    date_to: datetime
    blinded: bool = False
    notes: str | None = None


class SessionPatch(BaseModel):
    ended_at: datetime | None = None
    notes: str | None = None


class SessionOut(BaseModel):
    session_id: str
    started_at: str | None = None
    ended_at: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    blinded: bool = False
    notes: str | None = None


class TradeSubmit(BaseModel):
    session_id: str | None = None
    symbol: str
    timeframe: str
    signal_ts: datetime
    setup_id: str
    side: int = Field(..., description="+1 long, -1 short")
    entry: float
    sl: float
    tp: float
    notes: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    observed_result: str | None = None
    observed_trend: str | None = None
    confluence_tags: str | None = None
    session: str | None = None
    pips_captured: float | None = None
    screenshot_entry: str | None = None
    screenshot_exit: str | None = None
    metadata: dict | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class OccurrenceOut(BaseModel):
    id: str
    source: str
    session_id: str | None = None
    symbol: str
    timeframe: str
    ts: str
    setup_id: str
    side: int
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    max_bars: int | None = None
    atr_period: int | None = None
    atr_at_signal: float | None = None
    result: str | None = None
    realized_r: float | None = None
    bars_to_resolution: int | None = None
    observed_result: str | None = None
    trend_state: str | None = None
    atr_bucket: str | None = None
    session: str | None = None
    rsi_band: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    notes: str | None = None
    labeler_version: str | None = None
    pips_captured: float | None = None
    observed_trend: str | None = None
    confluence_tags: str | None = None
    screenshot_entry: str | None = None
    screenshot_exit: str | None = None
    metadata: dict | None = None
    created_at: str | None = None


class CompareContext(BaseModel):
    trend_state: str | None = None
    session: str | None = None
    atr_bucket: str | None = None
    rsi_band: str | None = None


class CompareRequest(BaseModel):
    setup_id: str
    symbol: str
    timeframe: str
    context: CompareContext
    source: str = "manual"
    min_samples: int | None = None


class CompareResponse(BaseModel):
    matched_count: int
    wins: int
    decided: int
    timeouts: int = 0
    win_rate: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    expectancy_r: float | None = None
    level_used: str
