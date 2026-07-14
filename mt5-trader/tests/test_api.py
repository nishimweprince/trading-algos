from __future__ import annotations

import json
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


def test_console_logs_full_execution_lifecycle_without_secrets(settings, adapter, capsys) -> None:
    signal = payload() | {"note": "log every execution detail"}
    app = create_app(settings, adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=signal,
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
        assert response.status_code == 200

    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    events = {record["event"]: record for record in records}

    assert events["signal_received"]["signal"]["note"] == "log every execution detail"
    assert events["mt5_request_prepared"]["request"]["symbol"] == "EURUSD"
    assert (
        events["mt5_request_prepared"]["request_diagnostics"]["comment"]["character_length"] == 31
    )
    assert events["mt5_request_prepared"]["request_diagnostics"]["comment"]["ascii"] is True
    assert events["mt5_order_check_completed"]["check"]["retcode"] == 0
    assert events["mt5_order_send_completed"]["result"]["retcode"] == 10009
    assert events["signal_execution_completed"]["response"]["outcome"] == "filled"
    assert settings.api_key.get_secret_value() not in output
    assert settings.password.get_secret_value() not in output


def test_console_logs_none_preflight_diagnostics(settings, adapter, capsys) -> None:
    adapter.check_result = None
    app = create_app(settings, adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload(),
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
        assert response.status_code == 503

    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    events = {record["event"]: record for record in records}
    failed = events["mt5_order_check_returned_none"]

    assert failed["last_error"] == [-1, "fake error"]
    assert failed["request_diagnostics"]["comment"] == {
        "value": failed["request"]["comment"],
        "character_length": 31,
        "utf8_byte_length": 31,
        "ascii": True,
        "type": "str",
    }
    assert failed["request_diagnostics"]["field_types"]["comment"] == "str"
    assert events["service_error_response"]["status_code"] == 503
    assert events["service_error_response"]["error"]["code"] == "mt5_preflight_unavailable"
    assert settings.api_key.get_secret_value() not in output
    assert settings.password.get_secret_value() not in output
