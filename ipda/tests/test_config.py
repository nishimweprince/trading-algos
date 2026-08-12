from __future__ import annotations

from pathlib import Path

import pytest
from ipda.config import load_settings, resolve_env_file, resolve_symbols_file
from pydantic import ValidationError

_MINIMAL_ENV = """\
DATA_API_URL=http://127.0.0.1:8000/v1/market-data/candles
QUOTE=EURUSD
MT5_SYMBOL=EURUSD
VOLUME=0.10
MT5_SIGNAL_API_KEY=test-api-key-with-16-characters
"""


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


def test_defaults_match_the_configured_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live parameters must hold without being restated in every env file."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(_MINIMAL_ENV, encoding="utf-8")

    settings = load_settings()

    assert settings.target_tf_minutes == 3
    assert settings.reversal_rsi_len == 14
    assert settings.reversal_oversold == 25.0
    assert settings.reversal_overbought == 75.0
    assert settings.stop_loss_pips == 40.0
    assert settings.take_profit_pips == 50.0
    assert settings.mfe_break_even_pips == 30.0
    assert settings.trading_sessions == ["tokyo", "new_york"]
    assert settings.notification_channels == frozenset({"TELEGRAM"})


def test_empty_trading_sessions_means_no_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(_MINIMAL_ENV + "TRADING_SESSIONS=\n", encoding="utf-8")

    settings = load_settings()

    assert settings.trading_sessions == []
    assert settings.session_windows() == []


def test_unknown_session_is_rejected_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(_MINIMAL_ENV + "TRADING_SESSIONS=london\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown trading session"):
        load_settings()


def test_unknown_notification_channel_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        _MINIMAL_ENV + "NOTIFICATION_CHANNELS=PIGEON\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="unknown NOTIFICATION_CHANNELS"):
        load_settings()


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


def test_inverted_reversal_levels_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        _MINIMAL_ENV + "REVERSAL_OVERSOLD=80\nREVERSAL_OVERBOUGHT=20\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="REVERSAL_OVERSOLD must be below"):
        load_settings()


def test_disabling_hard_targets_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSI yields no price level, so an order without fixed pip targets has no stop."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(_MINIMAL_ENV + "USE_HARD_TARGETS=false\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="USE_HARD_TARGETS must be true"):
        load_settings()
