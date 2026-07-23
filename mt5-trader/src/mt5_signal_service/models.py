from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExecutionType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalSource(StrEnum):
    TRADING_CENTRAL = "trading_central"
    AUTOCHARTIST = "autochartist"


class SignalState(StrEnum):
    RECEIVED = "received"
    EXECUTING = "executing"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    PLACED = "placed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    signal_id: UUID
    occurred_at: datetime
    execution_type: ExecutionType
    symbol: str = Field(min_length=1, max_length=64)
    direction: Direction
    volume: Decimal = Field(gt=0)
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    deviation_points: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    source: SignalSource
    ignore_signal_age: bool = True

    @field_validator("occurred_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_order_fields(self) -> SignalRequest:
        if self.execution_type is ExecutionType.MARKET:
            if self.entry_price is not None:
                raise ValueError("entry_price is prohibited for market orders")
            if self.expires_at is not None:
                raise ValueError("expires_at is prohibited for market orders")
        elif self.entry_price is None:
            raise ValueError("entry_price is required for limit and stop orders")
        return self

    def canonical_json(self) -> str:
        return self.model_dump_json(exclude_none=False)


class SignalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: UUID
    outcome: SignalState
    order_ticket: int | None = None
    deal_ticket: int | None = None
    executed_volume: Decimal | None = None
    execution_price: Decimal | None = None
    broker_retcode: int | None = None
    broker_comment: str | None = None
    processed_at: datetime
    reconciled: bool = False

    @field_validator("outcome")
    @classmethod
    def require_success_outcome(cls, value: SignalState) -> SignalState:
        if value not in {
            SignalState.FILLED,
            SignalState.PARTIALLY_FILLED,
            SignalState.PLACED,
        }:
            raise ValueError("response outcome must be a successful terminal state")
        return value


class SignalStatus(BaseModel):
    signal_id: UUID
    state: SignalState
    response: SignalResponse | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    details: dict[str, Any] | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)
