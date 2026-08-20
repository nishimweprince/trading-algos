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
    assert body["gross_realized_pips"] == body["realized_pips"]
    assert body["net_realized_pips"] == body["gross_realized_pips"] - body["realized_cost_pips"]
    assert body["net_realized_r"] == body["gross_realized_r"] - body["realized_cost_r"]
    assert "breakeven_pips_per_side" in body
    assert "cost_headroom_ratio" in body
    assert body["risk_mode"] == "fixed_qty"
    assert "suppressed_signal_count" in body
    assert all("qty" in pair for pair in body["trade_pairs"])
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


def test_backtest_fixed_stop_override_pins_every_stop(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "stop_mode": "fixed_pips",
            "fixed_stop_pips": 150,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stop_mode"] == "fixed_pips"
    assert body["fixed_stop_pips"] == 150.0
    entries = [event for event in body["events"] if event["kind"] == "entry"]
    assert entries
    assert {round(event["detail"]["sl_dist"], 6) for event in entries} == {15.0}


def test_backtest_fixed_stop_without_distance_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "stop_mode": "fixed_pips"},
    )
    assert response.status_code == 422
    assert "FIXED_STOP_PIPS" in response.json()["detail"]


def test_backtest_dollar_mode_requires_conversion(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "performance_unit": "dollars"},
    )
    assert response.status_code == 422
    assert "DOLLARS_PER_PIP_PER_QTY" in response.json()["detail"]


def test_backtest_cost_override_is_revalidated(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "swap_timezone": "Not/A_Zone"},
    )
    assert response.status_code == 422
    assert "SWAP_TIMEZONE" in response.json()["detail"]


def test_backtest_fixed_fractional_override_is_revalidated(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "risk_mode": "fixed_fractional"},
    )
    assert response.status_code == 422
    assert "DOLLARS_PER_PIP_PER_QTY" in response.json()["detail"]


def test_backtest_custom_firm_override_is_revalidated(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "firm_profile": "custom"},
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
    assert body["stop_mode"] == "bar_range"
    assert body["sl_mult"] == 2.0
    assert body["fixed_stop_pips"] == 0.0
    assert body["rr"] == 3.0
    assert body["min_stop_pips"] == 0.0
    assert body["qty"] == 1.0
    assert body["pip_size"] == 0.1
    assert body["point_value"] == 1.0
    assert body["orb_minutes"] == 60
    assert body["entry_delay_minutes"] == 15
    assert body["anchor_tolerance_minutes"] == 15
    assert body["intrabar_mode"] == "m1_conservative"
    assert body["performance_unit"] == "pips"
    assert body["dollars_per_pip_per_qty"] is None
    assert body["cost_model"] == "per_session"
    assert body["spread_pips_per_side"] == 0.0
    assert body["swap_timezone"] == "America/New_York"
    assert body["breakeven_cost_report"] is True
    assert body["risk_mode"] == "fixed_qty"
    assert body["risk_pct_per_r"] == 0.1
    assert body["max_pair_risk_pct"] == 0.2
    assert body["max_open_risk_pct"] == 0.75
    assert body["max_concurrent_structures"] == 3
    assert body["one_open_per_session"] is True
    assert body["firm_profile"] == "none"
    assert body["firm_initial_balance"] == 100_000
    assert body["firm_daily_loss_limit_pct"] == 5.0
    assert body["firm_total_loss_limit_pct"] == 10.0
    assert body["time_exit_mode"] == "max_age"
    assert body["max_age_hours"] == 24.0
    assert "api_key" not in body
    assert "ctrader_api_key" not in body


def test_paper_status_when_disabled(client: TestClient) -> None:
    response = client.get("/v1/paper")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["prop_guard_breached"] is False
