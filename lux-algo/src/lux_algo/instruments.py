"""Per-instrument configuration loaded from a symbols manifest file."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from .config import Settings


class InstrumentConfig(BaseModel):
    quote: str = Field(min_length=1)
    mt5_symbol: str | None = None
    pip_size: float | None = Field(default=None, gt=0)
    price_digits: int | None = Field(default=None, ge=0, le=10)
    volume: Decimal | None = Field(default=None, gt=0)
    deviation_points: int | None = Field(default=None, ge=0)
    stop_loss_pips: float | None = Field(default=None, gt=0)
    take_profit_pips: float | None = Field(default=None, gt=0)

    @field_validator("deviation_points", mode="before")
    @classmethod
    def empty_deviation_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def default_mt5_symbol(self) -> InstrumentConfig:
        # Mutate and return self. An after-validator that returns anything else
        # has its return value discarded by pydantic (with a UserWarning), so
        # the model_copy this replaces left mt5_symbol as None and every
        # resolved_mt5_symbol() below tripped its own assert.
        if self.mt5_symbol is None:
            self.mt5_symbol = self.quote
        return self

    def resolved_mt5_symbol(self) -> str:
        assert self.mt5_symbol is not None
        return self.mt5_symbol

    def resolved_pip_size(self, settings: Settings) -> float:
        if self.pip_size is not None:
            return self.pip_size
        return settings.pip_size

    def resolved_price_digits(self, settings: Settings) -> int:
        if self.price_digits is not None:
            return self.price_digits
        return settings.price_digits

    def resolved_volume(self, settings: Settings) -> Decimal:
        if self.volume is not None:
            return self.volume
        return settings.volume

    def resolved_deviation_points(self, settings: Settings) -> int | None:
        if self.deviation_points is not None:
            return self.deviation_points
        return settings.deviation_points

    def resolved_stop_loss_pips(self, settings: Settings) -> float:
        if self.stop_loss_pips is not None:
            return self.stop_loss_pips
        return settings.stop_loss_pips

    def resolved_take_profit_pips(self, settings: Settings) -> float:
        if self.take_profit_pips is not None:
            return self.take_profit_pips
        return settings.take_profit_pips


def load_instruments_from_file(path: Path) -> list[InstrumentConfig]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing symbols file: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("symbols file must be a JSON array of instrument objects")
    instruments = [InstrumentConfig.model_validate(item) for item in raw]
    _validate_unique_quotes(instruments)
    return instruments


def instrument_from_legacy(quote: str, mt5_symbol: str) -> InstrumentConfig:
    return InstrumentConfig(quote=quote, mt5_symbol=mt5_symbol)


def _validate_unique_quotes(instruments: list[InstrumentConfig]) -> None:
    seen: set[str] = set()
    for inst in instruments:
        if inst.quote in seen:
            raise ValueError(f"duplicate quote in symbols file: {inst.quote!r}")
        seen.add(inst.quote)


def instrument_summary(instrument: InstrumentConfig, settings: Settings) -> dict[str, Any]:
    return {
        "quote": instrument.quote,
        "mt5_symbol": instrument.resolved_mt5_symbol(),
        "pip_size": instrument.resolved_pip_size(settings),
        "price_digits": instrument.resolved_price_digits(settings),
        "volume": str(instrument.resolved_volume(settings)),
        "deviation_points": instrument.resolved_deviation_points(settings),
        "stop_loss_pips": instrument.resolved_stop_loss_pips(settings),
        "take_profit_pips": instrument.resolved_take_profit_pips(settings),
    }
