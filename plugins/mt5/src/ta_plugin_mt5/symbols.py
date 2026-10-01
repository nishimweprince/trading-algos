"""The MT5 symbol manifest: canonical quote names to exact broker symbols.

The same JSON file strategies use (``symbols.<profile>.json``). Broker symbol
names are case-sensitive and kept verbatim: Deriv names them "Volatility 75
Index" and "Step Index", and upper-casing would break every one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["MT5SymbolDefinition", "load_mt5_symbols"]


class MT5SymbolDefinition(BaseModel):
    """The execution-service subset of the shared strategy symbol manifest."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    quote: str = Field(min_length=1)
    mt5_symbol: str | None = Field(default=None, min_length=1)

    @field_validator("quote", "mt5_symbol")
    @classmethod
    def strip_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("symbol names must not be blank")
        return stripped

    @property
    def broker_symbol(self) -> str:
        return self.mt5_symbol or self.quote


def load_mt5_symbols(path: Path) -> tuple[str, ...]:
    """Load exact, case-sensitive broker symbols from a strategy-compatible JSON manifest."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing MT5 symbols manifest {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in MT5 symbols manifest {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"MT5 symbols manifest {path} must contain a non-empty JSON array")

    definitions = tuple(MT5SymbolDefinition.model_validate(item) for item in raw)
    symbols = tuple(definition.broker_symbol for definition in definitions)
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        raise ValueError(
            f"MT5 symbols manifest {path} contains duplicate broker symbols: {duplicates}"
        )
    return symbols
