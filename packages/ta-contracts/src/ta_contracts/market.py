"""Wire models shared by every service that speaks this platform's HTTP APIs.

Lifted verbatim from ctrader-markets/src/models.py. They were already duplicated
field-for-field in session-hedging and lookup-trader — both copies carried a
docstring pointing at the twin — and silent drift between them corrupts
backtests without failing anything. One definition removes that class of bug.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

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


# Minutes per bar, for every Timeframe above. Lives here rather than in a
# service because it is a property of the enum, not of any one consumer: the
# candle client needs it to size a range request, and the engine needs it to
# stamp bar boundaries.
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
    lot_size: int | None = None
    min_volume: int | None = None
    max_volume: int | None = None
    step_volume: int | None = None
    sl_distance: int | None = None
    trading_mode: int | None = None
    guaranteed_stop_loss: bool = False


class SymbolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[SymbolInfo]


class Environment(StrEnum):
    DEMO = "demo"
    LIVE = "live"
