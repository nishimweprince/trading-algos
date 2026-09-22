"""Configuration rules that belong to the MT5 adapter.

Ported from mt5-trader/tests/test_config.py. They live here rather than in the
shared settings tests because each one is only enforced when ADAPTERS names mt5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from execution_service.config import Settings

from .conftest import mt5_settings


def test_allowed_signal_sources_are_configurable(tmp_path: Path) -> None:
    settings = mt5_settings(tmp_path, allowed_signal_sources_csv="ipda, LUX_ALGO ")
    assert settings.allowed_signal_sources == frozenset({"ipda", "lux_algo"})


def test_allowed_signal_sources_rejects_invalid_slug(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="invalid slug"):
        mt5_settings(tmp_path, allowed_signal_sources_csv="trading central")


def test_symbol_case_is_preserved(tmp_path: Path) -> None:
    """MT5 symbol lookup is case-sensitive; Deriv names look like 'Step Index'."""
    settings = mt5_settings(tmp_path, allowed_symbols_csv="Step Index,Volatility 75 Index")
    assert settings.allowed_symbols == frozenset({"Step Index", "Volatility 75 Index"})


def test_symbols_file_loads_exact_mt5_symbols(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.hfm.json"
    symbols_file.write_text(
        json.dumps(
            [
                {"quote": "XAUUSD", "mt5_symbol": "XAUUSD.a"},
                {"quote": "GOLD"},
            ]
        ),
        encoding="utf-8",
    )

    settings = mt5_settings(
        tmp_path,
        allowed_symbols_csv="",
        symbols_file=symbols_file,
    )

    assert settings.allowed_symbols == frozenset({"XAUUSD.a", "GOLD"})


def test_symbols_file_and_inline_allowlist_are_mutually_exclusive(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.ftmo.json"
    symbols_file.write_text('[{"quote":"XAUUSD"}]', encoding="utf-8")

    with pytest.raises(ValidationError, match="either SYMBOLS_FILE or ALLOWED_SYMBOLS"):
        mt5_settings(tmp_path, symbols_file=symbols_file)


def test_symbols_file_rejects_duplicate_broker_symbols(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.json"
    symbols_file.write_text(
        '[{"quote":"GOLD","mt5_symbol":"XAUUSD"},{"quote":"XAUUSD"}]',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="duplicate broker symbols"):
        mt5_settings(tmp_path, allowed_symbols_csv="", symbols_file=symbols_file)


def test_default_deviation_cannot_exceed_maximum(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="DEFAULT_DEVIATION_POINTS"):
        mt5_settings(tmp_path, default_deviation_points=50, maximum_deviation_points=20)


def test_mt5_adapter_requires_its_own_configuration(tmp_path: Path) -> None:
    """A host that names mt5 but configures none of it must fail loudly."""
    with pytest.raises(ValidationError, match="requires"):
        Settings(adapters_csv="mt5", api_key="test-api-key-with-16-characters")


def test_ctrader_configuration_is_not_required_for_an_mt5_host(tmp_path: Path) -> None:
    """The whole point of adapter-scoped validation."""
    settings = mt5_settings(tmp_path)
    assert settings.client_id is None
    assert settings.adapters == ("mt5",)


def test_unknown_adapter_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unknown ADAPTERS"):
        mt5_settings(tmp_path, adapters_csv="mt5,ninjatrader")


def test_mt5_event_log_sits_beside_the_signal_log(tmp_path: Path) -> None:
    settings = mt5_settings(tmp_path)
    assert settings.events_log_path == settings.signals_log_path.parent / "events.jsonl"
