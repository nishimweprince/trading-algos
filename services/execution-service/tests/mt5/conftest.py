from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from ta_contracts import SignalRequest
from ta_plugin_mt5.execution import MT5Execution
from ta_plugin_mt5.testing import FakeMT5Adapter
from ta_store import ExecutionRepository

from execution_service.config import Settings
from execution_service.signals import SignalService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        # This fixture describes the Windows/MT5 deployment, so it says so:
        # the unified Settings requires each adapter's configuration only when
        # ADAPTERS names that adapter.
        adapters_csv="mt5",
        terminal_path=Path("C:/Program Files/MetaTrader 5/terminal64.exe"),
        login=123456,
        password="not-a-real-password",
        server="Broker-Demo",
        api_key="test-api-key-with-16-characters",
        allowed_symbols_csv="EURUSD,GBPUSD",
        maximum_volume="2.0",
        magic_number=234000,
        database_path=tmp_path / "signals.sqlite3",
        execution_database_path=tmp_path / "executions.sqlite3",
        signals_log_path=tmp_path / "logs" / "signals.jsonl",
        trading_enabled=True,
    )


@pytest.fixture
def adapter() -> FakeMT5Adapter:
    return FakeMT5Adapter()


@pytest.fixture
def repository(settings: Settings) -> ExecutionRepository:
    repository = ExecutionRepository(settings.execution_database_path)
    repository.initialize()
    return repository


def signal_service(
    settings: Settings,
    adapter: FakeMT5Adapter,
    repository: ExecutionRepository,
    **kwargs: Any,
) -> SignalService:
    return SignalService(settings, MT5Execution(adapter, settings), repository, **kwargs)


@pytest.fixture
def service(
    settings: Settings, adapter: FakeMT5Adapter, repository: ExecutionRepository
) -> SignalService:
    return signal_service(settings, adapter, repository)


@pytest.fixture
def signal_factory():
    def factory(**overrides: Any) -> SignalRequest:
        payload: dict[str, Any] = {
            "signal_id": str(uuid4()),
            "occurred_at": datetime.now(UTC).isoformat(),
            "execution_type": "market",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": "0.10",
            "source": "trading_central",
        }
        payload.update(overrides)
        return SignalRequest.model_validate(payload)

    return factory


def mt5_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """A minimal valid MT5 settings object, for tests that vary one field."""
    base: dict[str, Any] = {
        "adapters_csv": "mt5",
        "terminal_path": Path("C:/MT5/terminal64.exe"),
        "login": 12345678,
        "password": "secret-password",
        "server": "Broker-Demo",
        "api_key": "test-api-key-with-16-characters",
        "allowed_symbols_csv": "EURUSD",
        "maximum_volume": "1.00",
        "magic_number": 234000,
        "database_path": tmp_path / "signals.sqlite3",
        "execution_database_path": tmp_path / "executions.sqlite3",
        "signals_log_path": tmp_path / "logs" / "signals.jsonl",
    }
    base.update(overrides)
    return Settings(**base)
