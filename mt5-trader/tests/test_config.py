from __future__ import annotations

from pathlib import Path

import pytest

from mt5_signal_service.config import load_settings, resolve_env_file


def test_resolve_env_file_default() -> None:
    assert resolve_env_file(None) == Path(".env")


def test_resolve_env_file_named() -> None:
    assert resolve_env_file("deriv") == Path(".env.deriv")
    assert resolve_env_file("forex") == Path(".env.forex")


def test_load_settings_reads_profile_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env_content = """
MT5_TERMINAL_PATH=C:\\MT5\\terminal64.exe
MT5_LOGIN=12345678
MT5_PASSWORD=secret-password
MT5_SERVER=Broker-Demo
API_KEY=test-api-key-with-16-characters
ALLOWED_SYMBOLS=Volatility 75 Index,Step Index
MAXIMUM_VOLUME=1.00
MAGIC_NUMBER=234001
DATABASE_PATH=C:\\data\\signals-deriv.sqlite3
PORT=8001
DEFAULT_DEVIATION_POINTS=50
MAXIMUM_DEVIATION_POINTS=100
"""
    (tmp_path / ".env.deriv").write_text(env_content.strip() + "\n", encoding="utf-8")

    settings = load_settings("deriv")

    assert settings.profile == "deriv"
    assert settings.port == 8001
    assert settings.magic_number == 234001
    assert settings.allowed_symbols == frozenset({"Volatility 75 Index", "Step Index"})
    assert settings.default_deviation_points == 50
    assert settings.maximum_deviation_points == 100


def test_load_settings_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="Missing .env.deriv. Copy .env.example.deriv"):
        load_settings("deriv")

    with pytest.raises(FileNotFoundError, match="Missing .env. Copy .env.example.forex"):
        load_settings()
