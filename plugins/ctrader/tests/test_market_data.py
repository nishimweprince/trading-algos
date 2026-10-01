from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ta_contracts import Candle, SymbolInfo, Timeframe
from ta_core import ServiceError
from ta_plugin_api import EXECUTION_GROUP, MARKET_DATA_GROUP, MarketDataProvider, load_providers

from ta_plugin_ctrader import FACTORY
from ta_plugin_ctrader.accounts import AccountDefinition
from ta_plugin_ctrader.errors import CTraderError
from ta_plugin_ctrader.gateway import CTraderGateway
from ta_plugin_ctrader.market_data import CTraderMarketData
from ta_plugin_ctrader.proto import ProtoOASpotEvent
from ta_plugin_ctrader.settings import CTraderSettingsMixin
from ta_plugin_ctrader.symbols import SymbolCatalog

ACCOUNTS = (
    AccountDefinition(
        alias="forex_demo",
        ctid_trader_account_id=1001,
        environment="demo",
        instruments={"XAUUSD": "XAUUSD.r"},
    ),
    AccountDefinition(
        alias="deriv_demo",
        ctid_trader_account_id=2002,
        environment="demo",
        instruments={"EURUSD": "EURUSD"},
    ),
)


def _settings(tmp_path: Path, **overrides: object) -> CTraderSettingsMixin:
    values: dict[str, object] = {
        "CTRADER_CLIENT_ID": "id",
        "CTRADER_CLIENT_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "access",
        "TOKEN_CACHE_PATH": tmp_path / "token-cache.json",
        "accounts": ACCOUNTS,
        "default_market_data_account": "forex_demo",
    }
    values.update(overrides)
    return CTraderSettingsMixin(**values)  # type: ignore[arg-type]


def _ready(gateway: CTraderGateway, alias: str, symbol: str, symbol_id: int) -> None:
    account = gateway.account(alias)
    account.catalog = SymbolCatalog(
        [
            SymbolInfo(
                symbol=symbol,
                symbol_id=symbol_id,
                digits=2 if symbol == "XAUUSD" else 5,
                enabled=True,
                lot_size=100,
                min_volume=1,
                max_volume=10_000,
                step_volume=1,
            )
        ]
    )
    account.reconciled = True
    gateway._environment_ready["demo"].set()


@pytest.fixture
def provider(tmp_path: Path) -> CTraderMarketData:
    provider = FACTORY.market_data(_settings(tmp_path))
    _ready(provider.gateway, "forex_demo", "XAUUSD", 41)
    _ready(provider.gateway, "deriv_demo", "EURUSD", 1)
    return provider


def test_published_in_both_groups() -> None:
    assert load_providers(MARKET_DATA_GROUP, ["ctrader"])["ctrader"] is FACTORY
    assert load_providers(EXECUTION_GROUP, ["ctrader"])["ctrader"] is FACTORY


def test_is_a_market_data_provider_with_one_feed_per_account(
    provider: CTraderMarketData,
) -> None:
    assert isinstance(provider, MarketDataProvider)
    assert provider.feeds() == {None, "forex_demo", "deriv_demo"}


def test_market_data_needs_an_account_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ACCOUNTS_CONFIG_PATH"):
        FACTORY.market_data(_settings(tmp_path, accounts=()))


async def test_spot_events_become_quotes_on_the_right_feed(provider: CTraderMarketData) -> None:
    provider.gateway._handle_spot(
        ProtoOASpotEvent(
            ctidTraderAccountId=1001,
            symbolId=41,
            bid=200_012_000,
            ask=200_015_000,
            timestamp=1_772_445_600_000,
        )
    )

    quote = await provider.quote("forex_demo", "XAUUSD")

    assert (quote.symbol, quote.source_instrument, quote.provider) == (
        "XAUUSD",
        "XAUUSD.r",
        "ctrader",
    )
    assert (quote.bid, quote.ask, quote.spread) == (2000.12, 2000.15, 0.03)
    assert quote.ts == datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    with pytest.raises(ServiceError) as raised:
        await provider.quote("deriv_demo", "EURUSD")
    assert raised.value.code == "tick_unavailable"


async def test_a_symbol_from_another_account_is_not_allowed(
    provider: CTraderMarketData,
) -> None:
    with pytest.raises(ServiceError) as raised:
        await provider.quote("deriv_demo", "XAUUSD")

    assert (raised.value.status_code, raised.value.code) == (422, "symbol_not_allowed")


async def test_unknown_feed_is_rejected(provider: CTraderMarketData) -> None:
    with pytest.raises(ServiceError) as raised:
        await provider.quote("nope", "XAUUSD")

    assert raised.value.code == "account_not_allowed"


async def test_an_unreconciled_account_is_not_ready(provider: CTraderMarketData) -> None:
    provider.gateway.account("deriv_demo").reconciled = False

    with pytest.raises(ServiceError) as raised:
        provider.instruments("deriv_demo")

    assert (raised.value.status_code, raised.value.code) == (503, "broker_not_ready")


async def test_candles_go_through_the_gateway_for_the_feed_account(
    provider: CTraderMarketData, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    bar = Candle(
        ts=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        source_instrument="EURUSD",
    )

    async def fetch(**kwargs: object) -> tuple[Candle, ...]:
        calls.append(kwargs)
        return (bar,)

    monkeypatch.setattr(provider.gateway, "fetch_candles", fetch)

    candles = await provider.candles("deriv_demo", "EURUSD", Timeframe.H1, 10)

    assert candles == [bar]
    assert calls == [
        {
            "account_alias": "deriv_demo",
            "symbol": "EURUSD",
            "timeframe": Timeframe.H1,
            "count": 10,
            "to": None,
        }
    ]


async def test_broker_errors_on_candles_are_a_503(
    provider: CTraderMarketData, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(**_kwargs: object) -> tuple[Candle, ...]:
        raise CTraderError("CH_SYMBOL_NOT_FOUND", "gone")

    monkeypatch.setattr(provider.gateway, "fetch_candles", fail)

    with pytest.raises(ServiceError) as raised:
        await provider.candles(None, "XAUUSD", Timeframe.H1, 10)

    assert (raised.value.status_code, raised.value.code) == (503, "candles_unavailable")
    assert raised.value.details == {"error_code": "CH_SYMBOL_NOT_FOUND"}


def test_instruments_report_lot_increments(provider: CTraderMarketData) -> None:
    [gold] = provider.instruments(None)

    assert (gold.symbol, gold.source_instrument, gold.digits) == ("XAUUSD", "XAUUSD.r", 2)
    assert gold.price_increment == pytest.approx(0.01)
    assert (gold.quantity_increment, gold.min_quantity, gold.max_quantity) == (0.01, 0.01, 100.0)


def test_stream_filter_resolves_case_insensitively(provider: CTraderMarketData) -> None:
    assert provider.resolve_symbols(None, ["xauusd"]) == {"XAUUSD"}
    with pytest.raises(ServiceError, match="EURUSD"):
        provider.resolve_symbols(None, ["EURUSD"])


def test_every_fixed_length_timeframe_is_supported(provider: CTraderMarketData) -> None:
    assert set(provider.capabilities(None).timeframes) == set(Timeframe)
