from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from mt5_signal_service.config import Settings
from mt5_signal_service.models import SignalRequest
from mt5_signal_service.repository import SignalRepository
from mt5_signal_service.service import SignalExecutionService

from .fakes import FakeMT5Adapter


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        terminal_path=Path("C:/Program Files/MetaTrader 5/terminal64.exe"),
        login=123456,
        password="not-a-real-password",
        server="Broker-Demo",
        api_key="test-api-key-with-16-characters",
        allowed_symbols_csv="EURUSD,GBPUSD",
        maximum_volume="2.0",
        magic_number=234000,
        database_path=tmp_path / "signals.sqlite3",
        trading_enabled=True,
    )


@pytest.fixture
def adapter() -> FakeMT5Adapter:
    return FakeMT5Adapter()


@pytest.fixture
def repository(settings: Settings) -> SignalRepository:
    repository = SignalRepository(settings.database_path)
    repository.initialize()
    return repository


@pytest.fixture
def service(
    settings: Settings, adapter: FakeMT5Adapter, repository: SignalRepository
) -> SignalExecutionService:
    return SignalExecutionService(settings, adapter, repository)


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
        }
        payload.update(overrides)
        return SignalRequest.model_validate(payload)

    return factory
