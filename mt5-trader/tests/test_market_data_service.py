from __future__ import annotations

import pytest

from mt5_signal_service.errors import ServiceError
from mt5_signal_service.market_data_service import MarketDataService
from mt5_signal_service.models import Timeframe
from mt5_signal_service.mt5_adapter import ConnectionSnapshot

from .fakes import FakeMT5Adapter


@pytest.fixture
def multi_symbol_settings(settings):
    return settings.model_copy(
        update={
            "allowed_symbols_csv": "EURUSD,Volatility 75 Index,Step Index",
        }
    )


def test_probe_symbols_reports_ok_for_all_allowed(multi_symbol_settings) -> None:
    adapter = FakeMT5Adapter()
    service = MarketDataService(multi_symbol_settings, adapter)

    results = service.probe_symbols_sync(count=2)

    assert len(results) == 3
    assert all(result["ok"] for result in results)
    assert {result["symbol"] for result in results} == {
        "EURUSD",
        "Step Index",
        "Volatility 75 Index",
    }
    assert all(result["candle_count"] == 5 for result in results)
    assert all(result["latest_close"] is not None for result in results)


def test_probe_symbols_reports_failure_for_disconnected_terminal(multi_symbol_settings) -> None:
    adapter = FakeMT5Adapter()
    adapter.connection = ConnectionSnapshot(
        connected=False,
        login=adapter.connection.login,
        trade_allowed=adapter.connection.trade_allowed,
        expert_allowed=adapter.connection.expert_allowed,
    )
    service = MarketDataService(multi_symbol_settings, adapter)

    results = service.probe_symbols_sync(count=2)

    assert len(results) == 3
    assert all(not result["ok"] for result in results)
    assert all(result["error_code"] == "terminal_not_ready" for result in results)


def test_probe_symbols_reports_candles_unavailable(multi_symbol_settings) -> None:
    adapter = FakeMT5Adapter()
    adapter.rates = None
    service = MarketDataService(multi_symbol_settings, adapter)

    results = service.probe_symbols_sync(count=2)

    assert len(results) == 3
    assert all(not result["ok"] for result in results)
    assert all(result["error_code"] == "candles_unavailable" for result in results)


def test_probe_symbols_reports_symbol_not_allowed(settings) -> None:
    adapter = FakeMT5Adapter()
    service = MarketDataService(settings, adapter)

    with pytest.raises(ServiceError):
        service._get_candles_sync("XAUUSD", Timeframe.M1, 2)
