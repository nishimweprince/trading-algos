"""The plugin API's conformance kit, run against a gateway with no socket."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ta_contracts import TIMEFRAME_MINUTES, Candle, SymbolInfo, Timeframe
from ta_plugin_api.testing import ExecutionConformance, MarketDataConformance

from ta_plugin_ctrader import FACTORY
from ta_plugin_ctrader.accounts import AccountDefinition
from ta_plugin_ctrader.gateway import CTraderGateway
from ta_plugin_ctrader.proto import ProtoOASpotEvent
from ta_plugin_ctrader.settings import CTraderSettingsMixin
from ta_plugin_ctrader.symbols import SymbolCatalog

NOW = datetime(2026, 3, 2, 10, 7, 30, tzinfo=UTC)
ACCOUNTS = (
    AccountDefinition(
        alias="forex_demo",
        ctid_trader_account_id=1001,
        environment="demo",
        instruments={"XAUUSD": "XAUUSD.r"},
    ),
)


def _settings(tmp_path: Path) -> CTraderSettingsMixin:
    return CTraderSettingsMixin(  # type: ignore[call-arg]
        CTRADER_CLIENT_ID="id",
        CTRADER_CLIENT_SECRET="secret",
        CTRADER_ACCESS_TOKEN="access",
        TOKEN_CACHE_PATH=tmp_path / "token-cache.json",
        accounts=ACCOUNTS,
        default_market_data_account="forex_demo",
    )


def _ready(gateway: CTraderGateway) -> None:
    account = gateway.account("forex_demo")
    account.catalog = SymbolCatalog(
        [
            SymbolInfo(
                symbol="XAUUSD",
                symbol_id=41,
                digits=2,
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


async def _trendbars(
    *,
    account_alias: str,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    to: datetime | None,
) -> tuple[Candle, ...]:
    """What the gateway's trendbar fetch returns: closed bars up to `to`."""
    del account_alias
    step = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    end = (to or NOW).replace(minute=0, second=0, microsecond=0)
    return tuple(
        Candle(
            ts=end - step * offset,
            open=2000,
            high=2001,
            low=1999,
            close=2000.5,
            volume=1,
            provider="ctrader",
            source_instrument="XAUUSD.r" if symbol == "XAUUSD" else symbol,
        )
        for offset in reversed(range(count))
    )


class TestMarketDataConformance(MarketDataConformance):
    @pytest.fixture
    def provider(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        provider = FACTORY.market_data(_settings(tmp_path))
        _ready(provider.gateway)
        monkeypatch.setattr(provider.gateway, "fetch_candles", _trendbars)
        provider.gateway._handle_spot(
            ProtoOASpotEvent(
                ctidTraderAccountId=1001,
                symbolId=41,
                bid=200_012_000,
                ask=200_015_000,
                timestamp=int(NOW.timestamp() * 1000),
            )
        )
        return provider

    @pytest.fixture
    def feed(self) -> str:
        return "forex_demo"

    @pytest.fixture
    def symbol(self) -> str:
        return "XAUUSD"

    @pytest.fixture
    def timeframe(self) -> Timeframe:
        return Timeframe.H1

    @pytest.fixture
    def now(self) -> datetime:
        return NOW


class TestExecutionConformance(ExecutionConformance):
    @pytest.fixture
    def provider(self, tmp_path: Path):
        gateway = FACTORY.gateway(_settings(tmp_path))
        _ready(gateway)
        return FACTORY.execution(_settings(tmp_path), gateway=gateway)

    @pytest.fixture
    def account(self) -> str:
        return "forex_demo"

    @pytest.fixture
    def client_order_id_limit(self) -> int:
        return 50  # ProtoOANewOrderReq.clientOrderId
