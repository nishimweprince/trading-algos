"""Candle contract matches ctrader-markets (interval-end ``ts``, closed bars)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Timeframe(StrEnum):
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"
    M4 = "M4"
    M5 = "M5"
    M10 = "M10"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    H12 = "H12"
    D1 = "D1"
    W1 = "W1"


class PerformanceUnit(StrEnum):
    PIPS = "pips"
    DOLLARS = "dollars"


TIMEFRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M2: 2,
    Timeframe.M3: 3,
    Timeframe.M4: 4,
    Timeframe.M5: 5,
    Timeframe.M10: 10,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.H12: 720,
    Timeframe.D1: 1440,
    Timeframe.W1: 10080,
}


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


class Candle(BaseModel):
    """One closed candle, timestamped at the END of its UTC interval."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str = "ctrader"
    source_instrument: str
    spread: float | None = None
    spread_source: str | None = None

    _check_ts = field_validator("ts")(_require_timezone)


class CandlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    candles: list[Candle]
    source: Literal["local", "ctrader"] = "ctrader"


class EngineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pip_size: float = Field(default=0.1, gt=0)
    sl_mult: float = Field(default=2.0, gt=0)
    rr: float = Field(default=3.0, gt=0)
    min_stop_pips: float = Field(default=0.0, ge=0)
    lock_pips: float = Field(default=20.0, ge=0)
    qty: float = Field(default=1.0, gt=0)
    skip_doji: bool = True
    timeframe_minutes: int = Field(default=15, gt=0)
    orb_minutes: int = Field(default=60, gt=0)
    entry_delay_minutes: int = Field(default=15, ge=0)
    anchor_tolerance_minutes: int = Field(default=15, ge=0)
    initial_capital: float = Field(default=100_000.0, gt=0)
    point_value: float = Field(default=1.0, gt=0)
    performance_unit: PerformanceUnit = PerformanceUnit.PIPS
    dollars_per_pip_per_qty: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _orb_multiple_of_bar(self) -> EngineParams:
        if self.orb_minutes % self.timeframe_minutes != 0:
            raise ValueError("ORB_MINUTES must be a multiple of the bar timeframe")
        return self


class ClosedLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str
    side: Literal["long", "short"]
    entry: float
    exit: float
    pnl: float
    bucket: Literal["win", "be", "loss"]
    ts: datetime
    reason: str
    pair_id: str | None = None
    role: Literal["primary", "hedge", "unknown"] = "unknown"
    entry_ts: datetime | None = None
    pnl_pips: float | None = None
    pnl_dollars: float | None = None
    mae_pips: float | None = None
    mfe_pips: float | None = None
    mae_dollars: float | None = None
    mfe_dollars: float | None = None


class TradePairLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Literal["long", "short"]
    role: Literal["primary", "hedge", "unknown"]
    status: Literal["open", "closed"]
    exit: float | None = None
    exit_ts: datetime | None = None
    pnl_pips: float
    pnl_dollars: float | None = None
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    mae_dollars: float | None = None
    mfe_dollars: float | None = None
    bucket: Literal["win", "be", "loss"] | None = None
    reason: str | None = None


class TradePairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session: str
    entry: float
    entry_ts: datetime
    status: Literal["open", "partial", "closed"]
    primary: TradePairLeg | None = None
    hedge: TradePairLeg | None = None
    unknown_legs: list[TradePairLeg] = Field(default_factory=list)
    pnl_pips: float
    pnl_dollars: float | None = None


class OpenPairView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session: str
    entry: float
    sl_dist: float
    long_open: bool
    short_open: bool
    locked: bool
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    entry_ts: datetime


class EngineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "signal",
        "entry",
        "lock",
        "exit",
        "signal_skipped_anchor_drift",
        "bar_skipped_invalid",
    ]
    session: str
    ts: datetime
    detail: dict[str, object] = Field(default_factory=dict)


class SessionAnchorStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str
    skip_count: int = 0
    signal_count: int = 0
    anchor_drift_p50: float | None = None
    anchor_drift_max: float | None = None


class Stats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized: float = 0.0
    realized_pips: float = 0.0
    long_wins: int = 0
    long_be: int = 0
    long_loss: int = 0
    short_wins: int = 0
    short_be: int = 0
    short_loss: int = 0
    locks: int = 0


class BacktestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    performance_unit: PerformanceUnit
    orb_minutes: int
    entry_delay_minutes: int
    anchor_tolerance_minutes: int
    realized: float
    unrealized: float
    equity: float
    realized_pips: float
    unrealized_pips: float
    max_drawdown_pips: float
    realized_dollars: float | None
    unrealized_dollars: float | None
    equity_dollars: float | None
    max_drawdown_dollars: float | None
    long_wins: int
    long_be: int
    long_loss: int
    short_wins: int
    short_be: int
    short_loss: int
    locks: int
    open_pairs: int
    session_anchor_stats: list[SessionAnchorStats] = Field(default_factory=list)
    trades: list[ClosedLeg]
    trade_pairs: list[TradePairResult]
    events: list[EngineEvent]


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    sessions: list[str]
    lock_pips: float
    sl_mult: float
    rr: float
    min_stop_pips: float
    qty: float
    pip_size: float
    orb_minutes: int
    entry_delay_minutes: int
    anchor_tolerance_minutes: int
    performance_unit: PerformanceUnit
    dollars_per_pip_per_qty: float | None


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = "XAUUSD"
    timeframe: Timeframe = Timeframe.M15
    date_from: datetime | None = None
    date_to: datetime | None = None
    source: Literal["local", "ctrader"] | None = None
    lock_pips: float | None = Field(default=None, ge=0)
    sl_mult: float | None = Field(default=None, gt=0)
    rr: float | None = Field(default=None, gt=0)
    min_stop_pips: float | None = Field(default=None, ge=0)
    qty: float | None = Field(default=None, gt=0)
    sessions: list[str] | None = None
    performance_unit: PerformanceUnit | None = None
    orb_minutes: int | None = Field(default=None, gt=0)
    entry_delay_minutes: int | None = Field(default=None, ge=0)
    anchor_tolerance_minutes: int | None = Field(default=None, ge=0)

    @field_validator("date_from", "date_to")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone(value)


class PaperStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    last_ts: datetime | None
    open_pairs: list[OpenPairView]
    stats: Stats
    events: list[EngineEvent]
