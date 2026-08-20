from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import create_app
from config import Settings
from models import Timeframe

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
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


def test_live(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_backtest_local_fixture(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "timeframe": "M15", "source": "local"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "local"
    assert body["bar_count"] > 0
    assert body["performance_unit"] == "pips"
    assert isinstance(body["realized_pips"], float)
    assert isinstance(body["max_drawdown_pips"], float)
    assert body["realized_dollars"] is None
    assert body["trade_pairs"]
    assert "trades" in body  # Legacy flat closed-leg contract remains available.
    assert body["long_wins"] + body["long_be"] + body["long_loss"] + body[
        "short_wins"
    ] + body["short_be"] + body["short_loss"] + 2 * body["open_pairs"] >= 0
    assert "tokyo" in {event["session"] for event in body["events"] if event["kind"] == "entry"}


def test_backtest_risk_override(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "lock_pips": 5, "sl_mult": 2, "rr": 3},
    )
    assert response.status_code == 200
    assert response.json()["bar_count"] > 0


def test_backtest_sweep_fade_override(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "strategy_mode": "sweep_fade",
            "signal_delay_bars": 2,
            "trail_step_pips": 50,
            "max_stop_pips": 80,
            "max_open_pairs": 1,
            "flatten_at_session_end": True,
            "sessions": ["london", "new_york"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bar_count"] > 0
    assert body["open_pairs"] >= 0


def test_backtest_dollar_mode_requires_conversion(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "performance_unit": "dollars"},
    )
    assert response.status_code == 422
    assert "DOLLARS_PER_PIP_PER_QTY" in response.json()["detail"]


def test_candles_local(client: TestClient) -> None:
    response = client.get("/v1/candles?symbol=XAUUSD&timeframe=M15&source=local&count=10")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "local"
    assert len(body["candles"]) == 10


def test_service_config(client: TestClient) -> None:
    response = client.get("/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "XAUUSD"
    assert body["timeframe"] == "M15"
    assert body["sessions"] == ["tokyo", "london", "new_york"]
    assert body["lock_pips"] == 20.0
    assert body["sl_mult"] == 2.0
    assert body["rr"] == 3.0
    assert body["min_stop_pips"] == 0.0
    assert body["qty"] == 1.0
    assert body["pip_size"] == 0.1
    assert body["performance_unit"] == "pips"
    assert body["strategy_mode"] == "lock_survivor"
    assert body["signal_delay_bars"] == 0
    assert body["trail_step_pips"] == 0.0
    assert body["max_stop_pips"] == 0.0
    assert body["max_open_pairs"] == 0
    assert body["flatten_at_session_end"] is False
    assert body["dollars_per_pip_per_qty"] is None
    assert "api_key" not in body
    assert "ctrader_api_key" not in body


def test_paper_status_when_disabled(client: TestClient) -> None:
    response = client.get("/v1/paper")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
