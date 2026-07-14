from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from mt5_signal_service.api import create_app


def payload() -> dict[str, object]:
    return {
        "signal_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "execution_type": "market",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": "0.10",
    }


def test_api_auth_execution_status_and_health(settings, adapter) -> None:
    app = create_app(settings, adapter)
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok", "details": None}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        assert client.post("/v1/signals", json=payload()).status_code == 401
        signal = payload()
        response = client.post(
            "/v1/signals",
            json=signal,
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "filled"
        status = client.get(
            f"/v1/signals/{signal['signal_id']}",
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
        assert status.status_code == 200
        assert status.json()["state"] == "filled"


def test_api_returns_structured_validation_errors(settings, adapter) -> None:
    app = create_app(settings, adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload() | {"execution_type": "limit"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_readiness_is_503_when_trading_disabled(settings, adapter) -> None:
    disabled = settings.model_copy(update={"trading_enabled": False})
    with TestClient(create_app(disabled, adapter)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["details"]["trading_enabled"] is False
