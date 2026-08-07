"""Canonical application symbols mapped to provider-specific instrument IDs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

DEFAULT_CAPITAL_EPICS: dict[str, str] = {"XAUUSD": "GOLD"}


class InstrumentMappingError(ValueError):
    """A canonical symbol or provider instrument has no unambiguous mapping."""


def _capital_mapping(mapping: Mapping[str, str] | None = None) -> dict[str, str]:
    source = DEFAULT_CAPITAL_EPICS if mapping is None else mapping
    normalized = {
        str(symbol).strip().upper(): str(epic).strip().upper()
        for symbol, epic in source.items()
        if str(symbol).strip() and str(epic).strip()
    }
    duplicates = {epic for epic, count in Counter(normalized.values()).items() if count > 1}
    if duplicates:
        raise InstrumentMappingError(
            f"Capital EPICs must map to one canonical symbol; duplicates={sorted(duplicates)}"
        )
    return normalized


def capital_epic_for(symbol: str, mapping: Mapping[str, str] | None = None) -> str:
    """Resolve an application symbol such as XAUUSD to Capital's EPIC."""
    canonical = symbol.strip().upper()
    try:
        return _capital_mapping(mapping)[canonical]
    except KeyError as exc:
        raise InstrumentMappingError(
            f"No Capital EPIC is configured for canonical symbol {canonical!r}"
        ) from exc


def canonical_symbol_for_capital(epic: str, mapping: Mapping[str, str] | None = None) -> str:
    """Resolve a Capital EPIC back to the symbol used by datasets and models."""
    provider_instrument = epic.strip().upper()
    reverse = {value: key for key, value in _capital_mapping(mapping).items()}
    try:
        return reverse[provider_instrument]
    except KeyError as exc:
        raise InstrumentMappingError(
            f"No canonical symbol is configured for Capital EPIC {provider_instrument!r}"
        ) from exc


__all__ = [
    "DEFAULT_CAPITAL_EPICS",
    "InstrumentMappingError",
    "canonical_symbol_for_capital",
    "capital_epic_for",
]
