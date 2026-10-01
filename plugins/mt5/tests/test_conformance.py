"""The plugin API's conformance kit, run against the MT5 fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from ta_contracts import Timeframe
from ta_plugin_api.testing import ExecutionConformance, MarketDataConformance

from ta_plugin_mt5 import FACTORY
from ta_plugin_mt5.market_data import MT5MarketData
from ta_plugin_mt5.terminal import TickSnapshot
from ta_plugin_mt5.testing import FakeMT5Adapter

OFFSET = 3 * 3600
NOW = datetime(2026, 3, 2, 10, 7, 30, tzinfo=UTC)


class TestMarketDataConformance(MarketDataConformance):
    @pytest.fixture
    async def provider(self, tmp_path: Path):
        manifest = tmp_path / "symbols.json"
        manifest.write_text(json.dumps([{"quote": "XAUUSD", "mt5_symbol": "XAUUSDb"}]))
        adapter = FakeMT5Adapter()
        adapter.symbol = adapter.symbol.__class__(**{**adapter.symbol.__dict__, "name": "XAUUSDb"})
        adapter.tick = TickSnapshot(bid=2000.0, ask=2000.4, time=int(NOW.timestamp()) + OFFSET)
        first = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        adapter.rates = [
            {
                "time": int((first + timedelta(minutes=5 * i)).timestamp()) + OFFSET,
                "open": 2000.0,
                "high": 2001.0,
                "low": 1999.0,
                "close": 2000.5,
                "volume": 10,
            }
            for i in range(14)  # 09:00 … 10:05; the 10:05 bar is still forming
        ]
        settings = SimpleNamespace(
            symbols_file=manifest,
            mt5_server_utc_offset_seconds=OFFSET,
            mt5_quote_poll_seconds=3600.0,
            subscriber_queue_size=16,
            terminal_path=Path("C:/MT5/terminal64.exe"),
            login=1,
            password=None,
            server="Broker-Demo",
            mt5_timeout_ms=1000,
        )
        instance = MT5MarketData(adapter, settings, clock=lambda: NOW)
        await instance.start()
        yield instance
        await instance.close()

    @pytest.fixture
    def feed(self) -> None:
        return None

    @pytest.fixture
    def symbol(self) -> str:
        return "XAUUSD"

    @pytest.fixture
    def timeframe(self) -> Timeframe:
        return Timeframe.M5

    @pytest.fixture
    def now(self) -> datetime:
        return NOW


class TestExecutionConformance(ExecutionConformance):
    @pytest.fixture
    def provider(self):
        settings = SimpleNamespace(
            profile="hfm",
            login=123456,
            magic_number=234000,
            default_deviation_points=10,
            maximum_deviation_points=20,
            trading_enabled=True,
            live_trading_enabled=False,
            allowed_symbols=frozenset({"EURUSD"}),
            maximum_volume=Decimal("2.0"),
        )
        return FACTORY.execution(settings, terminal=FakeMT5Adapter())

    @pytest.fixture
    def account(self) -> str:
        return "hfm"

    @pytest.fixture
    def client_order_id_limit(self) -> int:
        return 31  # the terminal truncates order comments past this
