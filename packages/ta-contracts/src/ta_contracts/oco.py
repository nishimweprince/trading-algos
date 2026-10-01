"""One-cancels-other entry groups: a buy-stop and a sell-stop, first fill wins.

The payload hash of a group is ``sha256(OcoGroupRequest.model_dump_json())``
and is checked against stored groups on replay, so field names and order are
part of the contract. ``profile`` is the account alias the group runs on (an
MT5 host's profile); ``account`` is accepted on input as a synonym and never
appears in the dump.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class OcoGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID
    profile: str = Field(
        pattern=r"^[a-z][a-z0-9_-]*$",
        validation_alias=AliasChoices("profile", "account"),
    )
    occurred_at: datetime
    decision_at: datetime
    symbol: str = Field(min_length=1, max_length=64)
    volume: Decimal = Field(gt=0)
    upper_trigger: Decimal = Field(gt=0)
    lower_trigger: Decimal = Field(gt=0)
    stop_distance: Decimal = Field(gt=0)
    target_distance: Decimal = Field(gt=0)
    expires_at: datetime
    source: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=31)
    protection_policy: Literal["fill_relative"] = "fill_relative"

    @property
    def account(self) -> str:
        return self.profile

    @field_validator("occurred_at", "decision_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("OCO timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered_triggers(self) -> OcoGroupRequest:
        if self.lower_trigger >= self.upper_trigger:
            raise ValueError("lower_trigger must be below upper_trigger")
        if self.expires_at <= self.occurred_at:
            raise ValueError("expiry must follow submission")
        return self
