from __future__ import annotations

from pathlib import Path

import pytest

from lux_algo.config import load_settings, resolve_env_file, resolve_symbols_file


def test_resolve_env_file_default() -> None:
    assert resolve_env_file(None) == Path(".env")


def test_resolve_env_file_named() -> None:
    assert resolve_env_file("deriv") == Path(".env.deriv")
    assert resolve_env_file("forex") == Path(".env.forex")


def test_load_settings_reads_profile_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env_content = """
DATA_API_URL=http://127.0.0.1:8001/v1/market-data/candles
DATA_API_KEY=test-api-key-with-16-characters
QUOTE=Volatility 75 Index
MT5_SYMBOL=Volatility 75 Index
VOLUME=0.10
DEVIATION_POINTS=50
MT5_SIGNAL_API_URL=http://127.0.0.1:8001
MT5_SIGNAL_API_KEY=test-api-key-with-16-characters
LOGS_DIR=logs/deriv
PIP_SIZE=0.01
PRICE_DIGITS=2
"""
    (tmp_path / ".env.deriv").write_text(env_content.strip() + "\n", encoding="utf-8")

    settings = load_settings("deriv")

    assert settings.profile == "deriv"
    assert len(settings.instruments) == 1
    assert settings.instruments[0].quote == "Volatility 75 Index"
    assert settings.mt5_signal_api_url == "http://127.0.0.1:8001"
    assert settings.mt5_symbol == "Volatility 75 Index"
    assert settings.deviation_points == 50
    assert settings.logs_dir == Path("logs/deriv")


def test_resolve_symbols_file_named() -> None:
    assert resolve_symbols_file(Path(".env.forex"), Path("symbols.forex.json")) == Path(
        "symbols.forex.json"
    )


def test_load_settings_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="Missing .env.deriv. Copy .env.example.deriv"):
        load_settings("deriv")

    with pytest.raises(FileNotFoundError, match="Missing .env. Copy .env.example.forex"):
        load_settings()
