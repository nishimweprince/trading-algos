from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from execution_service.config import load_account_registry, load_settings


def _write_registry(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_account_registry_loads_canonical_symbol_maps(tmp_path: Path) -> None:
    path = tmp_path / "accounts.toml"
    _write_registry(
        path,
        """
default_market_data_account = "forex_demo"

[[accounts]]
alias = "forex_demo"
ctid_trader_account_id = 1001
environment = "demo"
enabled = true
instruments = { eurusd = "EUR/USD", gold = "XAUUSD" }

[[accounts]]
alias = "deriv_live"
ctid_trader_account_id = 2001
environment = "live"
enabled = false
instruments = { volatility_75 = "Volatility 75" }
""",
    )

    registry = load_account_registry(path)

    assert registry.default_market_data_account == "forex_demo"
    assert registry.accounts[0].instruments == {"EURUSD": "EUR/USD", "GOLD": "XAUUSD"}
    assert registry.accounts[1].environment == "live"


@pytest.mark.parametrize(
    "body",
    [
        """
default_market_data_account = "one"
[[accounts]]
alias = "one"
ctid_trader_account_id = 1001
environment = "demo"
instruments = { EURUSD = "EURUSD" }
[[accounts]]
alias = "one"
ctid_trader_account_id = 1002
environment = "demo"
instruments = { XAUUSD = "XAUUSD" }
""",
        """
default_market_data_account = "disabled"
[[accounts]]
alias = "disabled"
ctid_trader_account_id = 1001
environment = "demo"
enabled = false
instruments = { EURUSD = "EURUSD" }
""",
    ],
)
def test_account_registry_rejects_unsafe_topologies(tmp_path: Path, body: str) -> None:
    path = tmp_path / "accounts.toml"
    _write_registry(path, body)

    with pytest.raises(ValidationError):
        load_account_registry(path)


def test_production_settings_load_the_external_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "accounts.toml"
    _write_registry(
        registry_path,
        """
default_market_data_account = "forex_demo"
[[accounts]]
alias = "forex_demo"
ctid_trader_account_id = 1001
environment = "demo"
instruments = { EURUSD = "EUR/USD" }
""",
    )
    (tmp_path / ".env.production").write_text(
        "\n".join(
            [
                "CTRADER_CLIENT_ID=test-client-id",
                "CTRADER_CLIENT_SECRET=test-client-secret",
                "CTRADER_ACCESS_TOKEN=test-access-token",
                "CTRADER_REFRESH_TOKEN=test-refresh-token",
                "API_KEY=test-api-key-at-least-16",
                f"ACCOUNTS_CONFIG_PATH={registry_path}",
                "MAX_VOLUME_LOTS=0.10",
                "ALLOWED_ORDER_SOURCES=strategy_a,strategy_b",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings("production")

    assert settings.gateway_enabled
    assert settings.default_market_data_account == "forex_demo"
    assert settings.account("forex_demo").instruments["EURUSD"] == "EUR/USD"


def test_production_runtime_candidates_include_broker_discoverable_disabled_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "accounts.toml"
    _write_registry(
        registry_path,
        """
default_market_data_account = "demo"
[[accounts]]
alias = "demo"
ctid_trader_account_id = 1001
environment = "demo"
enabled = true
instruments = { EURUSD = "EURUSD" }
[[accounts]]
alias = "live"
ctid_trader_account_id = 2001
environment = "live"
enabled = false
instruments = { EURUSD = "EURUSD" }
""",
    )
    (tmp_path / ".env.production").write_text(
        "\n".join(
            [
                "CTRADER_CLIENT_ID=test-client-id",
                "CTRADER_CLIENT_SECRET=test-client-secret",
                "CTRADER_ACCESS_TOKEN=test-access-token",
                "API_KEY=test-api-key-at-least-16",
                f"ACCOUNTS_CONFIG_PATH={registry_path}",
                "MAX_VOLUME_LOTS=0.10",
                "ALLOWED_ORDER_SOURCES=strategy_a",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings("production")

    assert [account.alias for account in settings.enabled_accounts] == ["demo"]
    assert [account.alias for account in settings.gateway_accounts] == ["demo", "live"]
    assert settings.account("2001").alias == "live"
