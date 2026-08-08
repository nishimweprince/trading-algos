from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Timeframe(StrEnum):
    """ProtoOATrendbarPeriod names with a fixed duration.

    MN1 is deliberately absent: a calendar month has no constant length, so it
    cannot be stamped at interval-end from utcTimestampInMinutes alone.
    """

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


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


class Tick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    bid: float
    ask: float
    spread: float
    ts: datetime = Field(
        description="Server time when available, otherwise local clock. Aware UTC."
    )
    provider: Literal["ctrader"] = "ctrader"

    _check_ts = field_validator("ts")(_require_timezone)


class Candle(BaseModel):
    """One closed candle, timestamped at the END of its UTC interval.

    Field-identical to lookup-trader's app/providers/base.py::Candle so a client
    there can construct it with Candle(**payload) and no shape translation.

    cTrader sends ProtoOATrendbar.utcTimestampInMinutes as the interval START;
    the conversion to interval-end happens in decode.py.
    """

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


class SymbolInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    symbol_id: int
    digits: int
    enabled: bool
    description: str | None = None


class SymbolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[SymbolInfo]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    details: dict[str, Any] | None = None
