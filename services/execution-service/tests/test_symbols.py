from __future__ import annotations

import pytest
from ta_contracts import SymbolInfo

from execution_service.adapters.ctrader.proto import ProtoOALightSymbol
from execution_service.adapters.ctrader.symbols import SymbolCatalog
from execution_service.errors import SymbolResolutionError


def _catalog() -> SymbolCatalog:
    return SymbolCatalog(
        [
            SymbolInfo(symbol="EURUSD", symbol_id=1, digits=5, enabled=True),
            SymbolInfo(symbol="XAUUSD", symbol_id=2, digits=2, enabled=True),
        ]
    )


def test_resolves_both_directions() -> None:
    catalog = _catalog()
    assert catalog.id_for("EURUSD") == 1
    assert catalog.name_for_id(2) == "XAUUSD"
    assert catalog.digits_for("XAUUSD") == 2
    assert catalog.digits_for_id(1) == 5


def test_unknown_symbol_fails_closed_and_names_the_alternatives() -> None:
    with pytest.raises(SymbolResolutionError) as excinfo:
        _catalog().id_for("GBPUSD")
    assert "GBPUSD" in str(excinfo.value)
    assert "EURUSD" in str(excinfo.value)


def test_unknown_symbol_id_fails_closed() -> None:
    with pytest.raises(SymbolResolutionError, match="99"):
        _catalog().name_for_id(99)


def test_duplicate_symbol_names_are_rejected() -> None:
    """An ambiguous catalog cannot be resolved safely in either direction."""
    with pytest.raises(SymbolResolutionError, match="duplicate"):
        SymbolCatalog(
            [
                SymbolInfo(symbol="EURUSD", symbol_id=1, digits=5, enabled=True),
                SymbolInfo(symbol="EURUSD", symbol_id=2, digits=5, enabled=True),
            ]
        )


def test_ids_and_names_are_stable_sorted() -> None:
    catalog = _catalog()
    assert catalog.names() == ("EURUSD", "XAUUSD")
    assert catalog.ids() == [1, 2]


def test_resolve_many_accepts_a_known_subset() -> None:
    assert _catalog().resolve_many(["EURUSD"]) == frozenset({"EURUSD"})


def test_resolve_many_rejects_an_unknown_selection() -> None:
    with pytest.raises(SymbolResolutionError, match="GBPUSD"):
        _catalog().resolve_many(["EURUSD", "GBPUSD"])


def test_build_from_broker_messages() -> None:
    catalog = SymbolCatalog.build(
        requested=["EURUSD"],
        light_symbols=[
            ProtoOALightSymbol(symbolId=7, symbolName="EURUSD", enabled=True, description="Euro"),
            ProtoOALightSymbol(symbolId=8, symbolName="GBPUSD", enabled=True),
        ],
        digits_by_id={7: 5, 8: 5},
    )
    info = catalog.info("EURUSD")
    assert (info.symbol_id, info.digits, info.description) == (7, 5, "Euro")
    assert len(catalog) == 1


def test_build_fails_when_the_broker_does_not_expose_a_configured_symbol() -> None:
    with pytest.raises(SymbolResolutionError) as excinfo:
        SymbolCatalog.build(
            requested=["EURUSD", "Volatility 75 Index"],
            light_symbols=[ProtoOALightSymbol(symbolId=7, symbolName="EURUSD")],
            digits_by_id={7: 5},
        )
    assert "Volatility 75 Index" in str(excinfo.value)
    assert "--discover-symbols" in str(excinfo.value)


def test_build_fails_when_digits_are_missing() -> None:
    """Without digits, prices cannot be scaled — better to refuse than guess."""
    with pytest.raises(SymbolResolutionError, match="digits"):
        SymbolCatalog.build(
            requested=["EURUSD"],
            light_symbols=[ProtoOALightSymbol(symbolId=7, symbolName="EURUSD")],
            digits_by_id={},
        )
