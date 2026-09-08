from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from lux_algo.config import Settings, load_settings, resolve_symbols_file
from lux_algo.instruments import (
    InstrumentConfig,
    instrument_from_legacy,
    instrument_summary,
    load_instruments_from_file,
)


def test_load_instruments_from_file(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps(
            [
                {"quote": "XAUUSD", "pip_size": 0.10, "price_digits": 3, "volume": "0.05"},
                {"quote": "BTCUSD", "mt5_symbol": "BTCUSD"},
            ]
        ),
        encoding="utf-8",
    )
    instruments = load_instruments_from_file(path)
    assert len(instruments) == 2
    assert instruments[0].quote == "XAUUSD"
    assert instruments[0].mt5_symbol == "XAUUSD"
    assert instruments[1].quote == "BTCUSD"


def test_duplicate_quote_rejected(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps([{"quote": "XAUUSD"}, {"quote": "XAUUSD"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate quote"):
        load_instruments_from_file(path)


def test_resolved_fields_fall_back_to_settings() -> None:
    settings = Settings(
        data_api_url="https://data.example.com/candles",
        quote="EURUSD",
        mt5_symbol="EURUSD",
        volume=Decimal("0.10"),
        mt5_signal_api_key="unit-test-key",
        price_digits=5,
        pip_size_override=0.0001,
        deviation_points=20,
    )  # type: ignore[call-arg]
    instrument = InstrumentConfig(quote="XAUUSD", mt5_symbol="XAUUSD")
    assert instrument.resolved_pip_size(settings) == 0.0001
    assert instrument.resolved_price_digits(settings) == 5
    assert instrument.resolved_volume(settings) == Decimal("0.10")
    assert instrument.resolved_deviation_points(settings) == 20


def test_resolved_fields_use_instrument_overrides() -> None:
    settings = Settings(
        data_api_url="https://data.example.com/candles",
        quote="EURUSD",
        mt5_symbol="EURUSD",
        volume=Decimal("0.10"),
        mt5_signal_api_key="unit-test-key",
        price_digits=5,
        pip_size_override=0.0001,
        deviation_points=20,
        stop_loss_pips=25.0,
        take_profit_pips=40.0,
    )  # type: ignore[call-arg]
    instrument = InstrumentConfig(
        quote="XAUUSD",
        pip_size=0.10,
        price_digits=3,
        volume=Decimal("0.05"),
        deviation_points=50,
        stop_loss_pips=300.0,
        take_profit_pips=600.0,
    )
    assert instrument.resolved_pip_size(settings) == 0.10
    assert instrument.resolved_price_digits(settings) == 3
    assert instrument.resolved_volume(settings) == Decimal("0.05")
    assert instrument.resolved_deviation_points(settings) == 50
    assert instrument.resolved_stop_loss_pips(settings) == 300.0
    assert instrument.resolved_take_profit_pips(settings) == 600.0


def test_instrument_from_legacy() -> None:
    instrument = instrument_from_legacy("EURUSD", "EURUSD")
    assert instrument.quote == "EURUSD"
    assert instrument.mt5_symbol == "EURUSD"


def test_instrument_summary() -> None:
    settings = Settings(
        data_api_url="https://data.example.com/candles",
        quote="EURUSD",
        mt5_symbol="EURUSD",
        volume=Decimal("0.10"),
        mt5_signal_api_key="unit-test-key",
        price_digits=5,
        pip_size_override=0.0001,
    )  # type: ignore[call-arg]
    summary = instrument_summary(InstrumentConfig(quote="XAUUSD", pip_size=0.10), settings)
    assert summary["quote"] == "XAUUSD"
    assert summary["pip_size"] == 0.10


def test_resolve_symbols_file_relative_to_env_parent() -> None:
    assert resolve_symbols_file(Path(".env.forex"), Path("symbols.forex.json")) == Path(
        "symbols.forex.json"
    )


def test_load_settings_with_symbols_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    symbols = tmp_path / "symbols.forex.json"
    symbols.write_text(
        json.dumps(
            [
                {"quote": "XAUUSD", "pip_size": 0.10, "price_digits": 3},
                {"quote": "BTCUSD", "pip_size": 1.0, "price_digits": 2},
            ]
        ),
        encoding="utf-8",
    )
    env_content = """
DATA_API_URL=http://127.0.0.1:8000/v1/market-data/candles
SYMBOLS_FILE=symbols.forex.json
VOLUME=0.10
MT5_SIGNAL_API_URL=http://127.0.0.1:8000
MT5_SIGNAL_API_KEY=test-api-key-with-16-characters
PIP_SIZE=0.0001
PRICE_DIGITS=5
"""
    (tmp_path / ".env").write_text(env_content.strip() + "\n", encoding="utf-8")

    settings = load_settings()

    assert len(settings.instruments) == 2
    assert settings.instruments[0].quote == "XAUUSD"
    assert settings.instruments[1].quote == "BTCUSD"


def test_load_settings_legacy_single_quote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env_content = """
DATA_API_URL=http://127.0.0.1:8000/v1/market-data/candles
QUOTE=EURUSD
MT5_SYMBOL=EURUSD
VOLUME=0.10
MT5_SIGNAL_API_URL=http://127.0.0.1:8000
MT5_SIGNAL_API_KEY=test-api-key-with-16-characters
"""
    (tmp_path / ".env").write_text(env_content.strip() + "\n", encoding="utf-8")

    settings = load_settings()

    assert len(settings.instruments) == 1
    assert settings.instruments[0].quote == "EURUSD"
    assert settings.instruments[0].mt5_symbol == "EURUSD"
