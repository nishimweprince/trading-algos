from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import load_settings, resolve_env_file
from tests.conftest import ENV_TEMPLATE, build_settings


def test_resolve_env_file_defaults_to_dotenv() -> None:
    assert resolve_env_file(None) == Path(".env")
    assert resolve_env_file("deriv") == Path(".env.deriv")


def test_load_settings_missing_profile_names_the_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_settings("deriv")
    message = str(excinfo.value)
    assert ".env.deriv" in message
    assert ".env.example.deriv" in message


def test_load_settings_stamps_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.forex").write_text(ENV_TEMPLATE, encoding="utf-8")
    settings = load_settings("forex")
    assert settings.profile == "forex"
    assert settings.source == "ctrader-markets.forex"
    assert settings.symbols == frozenset({"EURUSD", "XAUUSD"})


def test_host_derived_from_environment(tmp_path: Path) -> None:
    assert build_settings(tmp_path).resolved_host == "demo.ctraderapi.com"
    live = build_settings(tmp_path, CTRADER_ENVIRONMENT="live")
    assert live.resolved_host == "live.ctraderapi.com"


def test_explicit_host_overrides_environment(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, CTRADER_HOST="broker.example.com")
    assert settings.resolved_host == "broker.example.com"


def test_rejects_unknown_environment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="demo or live"):
        build_settings(tmp_path, CTRADER_ENVIRONMENT="staging")


def test_heartbeat_interval_capped_below_the_protocol_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, HEARTBEAT_INTERVAL_SECONDS=12)


def test_historical_rate_capped_at_the_documented_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, HISTORICAL_REQUESTS_PER_SECOND=25)


def test_rejects_mn1_trendbar_period(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="MN1"):
        build_settings(tmp_path, LIVE_TRENDBAR_PERIODS="M1,MN1")


def test_rejects_empty_symbol_list(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, SYMBOLS=" , ")


def test_rejects_backoff_inversion(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="RECONNECT_MAX_BACKOFF_SECONDS"):
        build_settings(
            tmp_path,
            RECONNECT_INITIAL_BACKOFF_SECONDS=90,
            RECONNECT_MAX_BACKOFF_SECONDS=60,
        )


def test_short_api_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, API_KEY="too-short")


def test_symbols_preserve_case_and_strip_whitespace(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, SYMBOLS=" US 500 , EURUSD ")
    assert settings.symbols == frozenset({"US 500", "EURUSD"})


def test_empty_trendbar_periods_is_allowed(tmp_path: Path) -> None:
    assert build_settings(tmp_path, LIVE_TRENDBAR_PERIODS="").live_trendbar_periods == ()
