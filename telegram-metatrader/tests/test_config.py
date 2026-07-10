from __future__ import annotations

import pytest

from telegram_metatrader.config import load_config, resolve_symbol


def base_env() -> dict[str, str]:
    return {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "hash",
        "CHAT_ID": "@blacktrade",
    }


def test_config_defaults_to_dry_run(tmp_path):
    config = load_config(tmp_path, base_env())

    assert config.live_trading_enabled is False
    assert config.default_volume == 0.01
    assert config.chat_id == "@blacktrade"
    assert config.state_dir == tmp_path / "state"
    assert resolve_symbol(config.symbol_map, "gold") == "XAUUSD"


def test_config_accepts_symbol_map_json(tmp_path):
    env = base_env() | {"SYMBOL_MAP_JSON": '{"GOLD":"XAUUSDm","EURUSD":"EURUSD.pro"}'}
    config = load_config(tmp_path, env)

    assert resolve_symbol(config.symbol_map, "GOLD") == "XAUUSDm"
    assert resolve_symbol(config.symbol_map, "eur/usd") == "EURUSD.pro"


def test_config_rejects_invalid_symbol_json(tmp_path):
    env = base_env() | {"SYMBOL_MAP_JSON": "{nope"}

    with pytest.raises(ValueError, match="SYMBOL_MAP_JSON"):
        load_config(tmp_path, env)


def test_config_requires_telegram_credentials(tmp_path):
    env = {"TELEGRAM_API_HASH": "hash", "CHAT_ID": "@blacktrade"}

    with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
        load_config(tmp_path, env)

