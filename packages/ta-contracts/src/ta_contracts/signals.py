"""The legacy single-order signal contract.

`POST /v1/signals` is what lux-algo, ipda, signals-scrapper and lookup-trader
already speak, and none of them migrate in this pass. The shapes are carried
over unchanged so the compatibility shim in execution-service is a translation
rather than a new contract.

`SignalRequest.canonical_json` is load-bearing: it feeds the idempotency hash
that gates replay, and it omits unset distance fields so payloads written before
those fields existed keep hashing identically. Changing it re-executes
already-filled signals against the existing signals.db.

NOTE the Legacy* market shapes at the bottom. mt5-trader's /v1/candles and
/v1/tick do NOT return ta_contracts.market.Candle: they return epoch-int `time`
and int `volume`, with no provider, source_instrument or spread. That is a real
contract difference, not drift, so the two live side by side and the shim keeps
serving the legacy shape on the legacy path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .execution import Direction, ExecutionType
from .market import Timeframe


class SignalSource(StrEnum):
    TRADING_CENTRAL = "trading_central"
    AUTOCHARTIST = "autochartist"
    LUX_ALGO = "lux_algo"
    IPDA = "ipda"


DEFAULT_SIGNAL_SOURCES = ",".join(member.value for member in SignalSource)
SOURCE_SLUG_PATTERN = r"^[a-z][a-z0-9_]*$"


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
    stop_loss_distance: Decimal | None = Field(default=None, gt=0)
    take_profit_distance: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    deviation_points: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    source: str = Field(
        min_length=1,
        max_length=31,
        pattern=SOURCE_SLUG_PATTERN,
        description=(
            "Signal provider slug written to the MT5 order comment. Accepted values are "
            "configured per instance via ALLOWED_SIGNAL_SOURCES."
        ),
    )
    ignore_signal_age: bool = True

    @field_validator("occurred_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone offset")
        return value

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
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
        if self.stop_loss is not None and self.stop_loss_distance is not None:
            raise ValueError("stop_loss and stop_loss_distance are mutually exclusive")
        if self.take_profit is not None and self.take_profit_distance is not None:
            raise ValueError("take_profit and take_profit_distance are mutually exclusive")
        return self

    def canonical_json(self) -> str:
        # Unused distance fields are omitted so that payloads written before these
        # fields existed keep hashing identically; the hash gates idempotent replay.
        unset_distances = {
            name
            for name in ("stop_loss_distance", "take_profit_distance")
            if getattr(self, name) is None
        }
        return self.model_dump_json(exclude_none=False, exclude=unset_distances or None)


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


class LegacyCandle(BaseModel):
    """mt5-trader's candle shape. Epoch seconds, integer volume, no provenance."""

    model_config = ConfigDict(extra="forbid")

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int


class LegacyCandlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    candles: list[LegacyCandle]


class LegacyTickResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    bid: float
    ask: float
