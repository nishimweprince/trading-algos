import pytest

from app.providers.instruments import (
    InstrumentMappingError,
    canonical_symbol_for_capital,
    capital_epic_for,
)


def test_xauusd_maps_to_capital_gold_bidirectionally():
    assert capital_epic_for("XAUUSD") == "GOLD"
    assert capital_epic_for("xauusd") == "GOLD"
    assert canonical_symbol_for_capital("GOLD") == "XAUUSD"
    assert canonical_symbol_for_capital("gold") == "XAUUSD"


def test_configured_mapping_is_used_without_changing_canonical_symbol():
    mapping = {"XAUUSD": "GOLD", "EURUSD": "EURUSD"}
    assert capital_epic_for("EURUSD", mapping) == "EURUSD"
    assert canonical_symbol_for_capital("GOLD", mapping) == "XAUUSD"


def test_unknown_or_ambiguous_instruments_fail_closed():
    with pytest.raises(InstrumentMappingError, match="No Capital EPIC"):
        capital_epic_for("GBPUSD")
    with pytest.raises(InstrumentMappingError, match="No canonical symbol"):
        canonical_symbol_for_capital("SILVER")
    with pytest.raises(InstrumentMappingError, match="duplicates"):
        capital_epic_for("XAUUSD", {"XAUUSD": "GOLD", "GOLDUSD": "GOLD"})
    with pytest.raises(InstrumentMappingError, match="No Capital EPIC"):
        capital_epic_for("XAUUSD", {})
