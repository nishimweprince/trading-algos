"""Strategy selection over HTTP: ipda/fu backtests share the report contract."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backtesting_service.api import create_app
from backtesting_service.config import Settings
from backtesting_service.models import Timeframe

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        paper_enabled=False,
        api_key=None,
    )
    store_dir = settings.local_candles_path("XAUUSD", Timeframe.M15)
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    store_dir.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_config_lists_strategies(client: TestClient) -> None:
    body = client.get("/v1/config").json()
    assert {"session_hedge", "ipda", "fu"} <= set(body["strategies"])


def test_ipda_backtest_uses_report_contract(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "timeframe": "M15", "source": "local", "strategy": "ipda"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "ipda"
    assert body["report_header"]["strategy"] == "ipda"
    assert len(body["candle_set_sha256"]) == 64
    assert body["effective_settings"]["strategy_params"]["rsi_len"] == 14
    assert "trade_pairs" in body and "events" in body and "performance" in body


def test_fu_backtest_uses_report_contract(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "source": "local",
            "strategy": "fu",
            "fu": {"rr_target": 3.0},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "fu"
    assert body["effective_settings"]["strategy_params"]["rr_target"] == 3.0


def test_same_range_same_fingerprint_across_strategies(client: TestClient) -> None:
    base = {"symbol": "XAUUSD", "timeframe": "M15", "source": "local"}
    first = client.post("/v1/backtests", json={**base, "strategy": "ipda"}).json()
    second = client.post("/v1/backtests", json={**base, "strategy": "fu"}).json()
    assert first["candle_set_sha256"] == second["candle_set_sha256"]
    assert first["bar_count"] == second["bar_count"]


def test_unknown_strategy_is_422(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "timeframe": "M15", "source": "local", "strategy": "nope"},
    )
    assert response.status_code == 422


def test_compare_rejects_signal_strategies(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests/compare",
        json={"symbol": "XAUUSD", "timeframe": "M15", "source": "local", "strategy": "ipda"},
    )
    assert response.status_code == 422


def test_fu_confluence_rejected_with_actionable_message(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "source": "local",
            "strategy": "fu",
            "fu": {"fu_only": False},
        },
    )
    assert response.status_code == 422
