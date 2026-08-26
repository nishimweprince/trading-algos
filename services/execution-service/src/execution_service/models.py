from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


def _require_unique_target_accounts(targets: list[Any]) -> list[Any]:
    aliases = [target.account for target in targets]
    if len(aliases) != len(set(aliases)):
        raise ValueError("targets must not repeat an account alias")
    return targets


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


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    details: dict[str, Any] | None = None


class Environment(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExecutionType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    GTC = "gtc"
    GTD = "gtd"


class OperationAction(StrEnum):
    PLACE_ORDER = "place_order"
    AMEND_ORDER = "amend_order"
    CANCEL_ORDER = "cancel_order"
    AMEND_POSITION = "amend_position"
    CLOSE_POSITION = "close_position"


class TargetState(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    PLACED = "placed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    AMENDED = "amended"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OperationState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILURE = "partial_failure"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_id: UUID
    occurred_at: datetime
    source: str = Field(min_length=1, max_length=31, pattern=r"^[a-z][a-z0-9_]*$")

    _check_occurred_at = field_validator("occurred_at")(_require_timezone)

    @field_validator("source", mode="before")
    @classmethod
    def normalize_operation_source(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class OrderTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    volume_lots: Decimal = Field(gt=0)


class OrderRequest(OperationBase):
    instrument: str = Field(min_length=1, max_length=64)
    execution_type: ExecutionType
    direction: Direction
    targets: list[OrderTarget] = Field(min_length=1)
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    stop_loss_distance: Decimal | None = Field(default=None, gt=0)
    take_profit_distance: Decimal | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.GTC
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_timezone(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, targets: list[OrderTarget]) -> list[OrderTarget]:
        aliases = [target.account for target in targets]
        if len(aliases) != len(set(aliases)):
            raise ValueError("targets must not repeat an account alias")
        return targets

    @field_validator("instrument", mode="before")
    @classmethod
    def normalize_instrument(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("time_in_force")
    @classmethod
    def validate_time_in_force(cls, value: TimeInForce) -> TimeInForce:
        return value

    @model_validator(mode="after")
    def validate_order_shape(self) -> OrderRequest:
        if self.execution_type is ExecutionType.MARKET:
            if self.entry_price is not None or self.expires_at is not None:
                raise ValueError("market orders prohibit entry_price and expires_at")
            if self.time_in_force is TimeInForce.GTD:
                raise ValueError("market orders prohibit GTD time in force")
        elif self.entry_price is None:
            raise ValueError("limit and stop orders require entry_price")
        if self.time_in_force is TimeInForce.GTD and self.expires_at is None:
            raise ValueError("GTD orders require expires_at")
        if self.time_in_force is TimeInForce.GTC and self.expires_at is not None:
            raise ValueError("expires_at requires GTD time in force")
        if self.stop_loss is not None and self.stop_loss_distance is not None:
            raise ValueError("stop_loss and stop_loss_distance are mutually exclusive")
        if self.take_profit is not None and self.take_profit_distance is not None:
            raise ValueError("take_profit and take_profit_distance are mutually exclusive")
        return self


class OrderReferenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    order_id: int = Field(gt=0)


class CancelOrderRequest(OperationBase):
    targets: list[OrderReferenceTarget] = Field(min_length=1)

    _unique_accounts = field_validator("targets")(_require_unique_target_accounts)


class AmendOrderTarget(OrderReferenceTarget):
    volume_lots: Decimal | None = Field(default=None, gt=0)
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None


class AmendOrderRequest(OperationBase):
    targets: list[AmendOrderTarget] = Field(min_length=1)

    _unique_accounts = field_validator("targets")(_require_unique_target_accounts)


class PositionProtectionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    position_id: int = Field(gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    trailing_stop_loss: bool = False


class PositionProtectionRequest(OperationBase):
    targets: list[PositionProtectionTarget] = Field(min_length=1)

    _unique_accounts = field_validator("targets")(_require_unique_target_accounts)


class ClosePositionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    position_id: int = Field(gt=0)
    volume_lots: Decimal = Field(gt=0)


class ClosePositionRequest(OperationBase):
    targets: list[ClosePositionTarget] = Field(min_length=1)

    _unique_accounts = field_validator("targets")(_require_unique_target_accounts)


class TargetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str
    state: TargetState
    order_id: int | None = None
    position_id: int | None = None
    deal_id: int | None = None
    executed_volume_lots: Decimal | None = None
    execution_price: Decimal | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime


class OperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    action: OperationAction
    state: OperationState
    targets: list[TargetResult]
    created_at: datetime
    updated_at: datetime


class BrokerOrder(BaseModel):
    account: str
    order_id: int
    position_id: int | None = None
    client_order_id: str | None = None
    instrument: str | None = None
    volume_lots: Decimal | None = None
    state: str


class BrokerPosition(BaseModel):
    account: str
    position_id: int
    instrument: str | None = None
    volume_lots: Decimal | None = None
    direction: Direction | None = None
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
