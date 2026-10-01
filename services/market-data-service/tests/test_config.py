from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ta_contracts import MarketKind

from market_data_service.config import MarketBinding, load_markets
from market_data_service.router import MarketRouter
from tests.conftest import FakeProvider, build_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(REPO_ROOT.glob(".env.example.*"))


def test_markets_load_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "markets.toml"
    path.write_text(
        '[forex]\nprovider = "ctrader"\nfeed = "forex_demo"\n\n[deriv]\nprovider = "mt5"\n',
        encoding="utf-8",
    )

    assert load_markets(path) == {
        MarketKind.FOREX: MarketBinding(provider="ctrader", feed="forex_demo"),
        MarketKind.DERIV: MarketBinding(provider="mt5"),
    }


def test_an_unknown_market_table_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "markets.toml"
    path.write_text('[stocks]\nprovider = "mt5"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown markets"):
        load_markets(path)


@pytest.mark.parametrize(
    ("market", "provider"),
    [(MarketKind.CRYPTO, "mt5"), (MarketKind.FOREX, "binance"), (MarketKind.DERIV, "deriv")],
)
def test_market_policy_rejects_a_provider_it_does_not_allow(
    tmp_path: Path, market: MarketKind, provider: str
) -> None:
    with pytest.raises(ValidationError, match="cannot be served by"):
        build_settings(tmp_path, markets={market: MarketBinding(provider=provider)})


def test_settings_are_view_only_whatever_the_environment_says(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True)

    assert settings.trading_enabled is False
    assert settings.live_trading_enabled is False


def test_validate_providers_needs_at_least_one_market(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one market"):
        build_settings(tmp_path, markets={}).validate_providers()


def test_validate_providers_reports_missing_plugin_settings(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, markets={MarketKind.FOREX: MarketBinding(provider="mt5")})

    with pytest.raises(ValueError, match="provider mt5 requires: MT5_TERMINAL_PATH"):
        settings.validate_providers()


def test_router_rejects_a_feed_the_provider_does_not_have() -> None:
    with pytest.raises(ValueError, match="no feed 'forex_live'.*forex_demo"):
        MarketRouter(
            {MarketKind.FOREX: MarketBinding(provider="ctrader", feed="forex_live")},
            {"ctrader": FakeProvider("ctrader", feeds=(None, "forex_demo"))},
        )


def test_the_example_profiles_exist() -> None:
    assert {p.name for p in EXAMPLES} == {
        ".env.example.ctrader",
        ".env.example.hfm",
        ".env.example.ftmo",
        ".env.example.deriv",
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_every_documented_key_is_a_real_setting(example: Path) -> None:
    """extra='ignore' silently drops a stale or misspelled key, so check here."""
    from pydantic import AliasChoices

    from market_data_service.config import Settings

    aliases: set[str] = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, str):
            aliases.add(alias)
        elif isinstance(alias, AliasChoices):
            aliases.update(choice for choice in alias.choices if isinstance(choice, str))
    keys = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert keys <= aliases, f"{example.name} documents unknown settings: {keys - aliases}"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_every_example_names_an_existing_markets_file(example: Path) -> None:
    keys = dict(
        line.split("=", 1)
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    markets_example = keys["MARKETS_CONFIG_PATH"].strip().replace("data/", "", 1)
    markets_example = markets_example.replace(".toml", ".example.toml")

    assert load_markets(REPO_ROOT / markets_example)


def test_load_settings_layers_the_registry_and_markets_for_ctrader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from market_data_service.config import load_settings

    (tmp_path / "data").mkdir()
    (tmp_path / "data/accounts.toml").write_text(
        (REPO_ROOT / "accounts.ctrader.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "data/markets.toml").write_text(
        (REPO_ROOT / "markets.ctrader.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".env.ctrader").write_text(
        "\n".join(
            [
                "API_KEY=test-api-key-at-least-16",
                "CTRADER_CLIENT_ID=id",
                "CTRADER_CLIENT_SECRET=secret",
                "CTRADER_ACCESS_TOKEN=access",
                "ACCOUNTS_CONFIG_PATH=data/accounts.toml",
                "MARKETS_CONFIG_PATH=data/markets.toml",
                "TRADING_ENABLED=true",
                "BINANCE_SYMBOLS=btcusdt, ETHUSDT,BTCUSDT",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings("ctrader")

    assert settings.profile == "ctrader"
    assert settings.providers == ("binance", "ctrader")
    assert settings.markets[MarketKind.CRYPTO] == MarketBinding(provider="binance")
    assert settings.binance_symbols == ("BTCUSDT", "ETHUSDT")
    assert settings.markets[MarketKind.DERIV] == MarketBinding(
        provider="ctrader", feed="deriv_demo"
    )
    assert [a.alias for a in settings.accounts] == ["forex_demo", "deriv_demo"]
    assert settings.token_cache_path == Path("data/token-cache.ctrader.json")
    assert settings.trading_enabled is False


def test_load_settings_rejects_an_mt5_profile_without_its_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from market_data_service.config import load_settings

    (tmp_path / "markets.toml").write_text('[deriv]\nprovider = "mt5"\n', encoding="utf-8")
    (tmp_path / ".env.deriv").write_text(
        "API_KEY=test-api-key-at-least-16\nMARKETS_CONFIG_PATH=markets.toml\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="provider mt5 requires: MT5_TERMINAL_PATH"):
        load_settings("deriv")


def test_load_settings_requires_a_markets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from market_data_service.config import load_settings

    (tmp_path / ".env.x").write_text("API_KEY=test-api-key-at-least-16\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="MARKETS_CONFIG_PATH"):
        load_settings("x")


def test_crypto_without_symbols_names_the_missing_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from market_data_service.config import load_settings

    (tmp_path / "markets.toml").write_text('[crypto]\nprovider = "binance"\n', encoding="utf-8")
    (tmp_path / ".env.crypto").write_text(
        "API_KEY=test-api-key-at-least-16\nMARKETS_CONFIG_PATH=markets.toml\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="BINANCE_SYMBOLS"):
        load_settings("crypto")
