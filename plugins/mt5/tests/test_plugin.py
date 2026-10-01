from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from ta_plugin_api import EXECUTION_GROUP, load_providers

from ta_plugin_mt5 import FACTORY
from ta_plugin_mt5.symbols import load_mt5_symbols
from ta_plugin_mt5.terminal import MT5Adapter
from ta_plugin_mt5.testing import FakeMT5Adapter


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "terminal_path": Path("C:/MT5/terminal64.exe"),
        "login": 123456,
        "password": SecretStr("secret"),
        "server": "Broker-Demo",
        "mt5_timeout_ms": 60_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_published_as_mt5_in_the_execution_group() -> None:
    assert load_providers(EXECUTION_GROUP, ["mt5"]) == {"mt5": FACTORY}


def test_complete_terminal_settings_are_not_missing_anything() -> None:
    assert FACTORY.missing_settings(_settings()) == []


def test_reports_missing_terminal_settings_by_environment_name() -> None:
    settings = _settings(terminal_path=None, server=None)

    assert FACTORY.missing_settings(settings) == ["MT5_TERMINAL_PATH", "MT5_SERVER"]


@pytest.mark.skipif(sys.platform == "win32", reason="MetaTrader5 may be installed")
def test_the_real_terminal_refuses_to_start_without_metatrader5() -> None:
    with pytest.raises(RuntimeError, match="only on the Windows"):
        FACTORY.terminal()


def test_the_fake_implements_every_terminal_method() -> None:
    required = {
        name
        for name, member in inspect.getmembers(MT5Adapter)
        if callable(member) and not name.startswith("_")
    }
    assert required <= set(dir(FakeMT5Adapter))
    assert required >= {"initialize", "order_send", "copy_rates", "symbol_tick"}


def test_symbol_manifest_keeps_broker_case(tmp_path: Path) -> None:
    manifest = tmp_path / "symbols.json"
    manifest.write_text(
        json.dumps(
            [
                {"quote": "XAUUSD", "mt5_symbol": "XAUUSD.r"},
                {"quote": "Volatility 75 Index"},
            ]
        ),
        encoding="utf-8",
    )

    assert load_mt5_symbols(manifest) == ("XAUUSD.r", "Volatility 75 Index")


def test_symbol_manifest_rejects_duplicate_broker_symbols(tmp_path: Path) -> None:
    manifest = tmp_path / "symbols.json"
    manifest.write_text(
        json.dumps([{"quote": "GOLD", "mt5_symbol": "XAUUSD"}, {"quote": "XAUUSD"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate broker symbols"):
        load_mt5_symbols(manifest)
