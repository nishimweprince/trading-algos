from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from execution_service.api import create_app
from execution_service.notifications import NotificationClient


def payload() -> dict[str, object]:
    return {
        "signal_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "execution_type": "market",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": "0.10",
        "source": "trading_central",
    }


def test_api_auth_execution_status_and_health(settings, adapter) -> None:
    app = create_app(settings, mt5_adapter=adapter)
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
    app = create_app(settings, mt5_adapter=adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload() | {"execution_type": "limit"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_api_accepts_configured_signal_source(settings, adapter) -> None:
    app = create_app(settings, mt5_adapter=adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload() | {"source": "ipda"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 200
    assert response.json()["outcome"] == "filled"


def test_api_rejects_source_outside_allowlist(settings, adapter) -> None:
    restricted = settings.model_copy(update={"allowed_signal_sources_csv": "trading_central"})
    app = create_app(restricted, mt5_adapter=adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload() | {"source": "ipda"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_not_allowed"


def test_validation_failure_is_logged_to_console_and_notified(settings, adapter, capsys) -> None:
    with patch.object(
        NotificationClient, "notify_request_failure", new_callable=AsyncMock
    ) as notify:
        app = create_app(settings, mt5_adapter=adapter)
        with TestClient(app) as client:
            response = client.post(
                "/v1/signals",
                json=payload() | {"execution_type": "limit"},
                headers={"X-API-Key": settings.api_key.get_secret_value()},
            )
    assert response.status_code == 422

    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    events = {record["event"]: record for record in records}
    assert "request_validation_failed" in events
    assert events["request_validation_failed"]["path"] == "/v1/signals"

    events_path = settings.signals_log_path.parent / "events.jsonl"
    file_records = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["event"] == "request_validation_failed" for record in file_records)
    notify.assert_awaited()


def test_readiness_is_503_when_trading_disabled(settings, adapter) -> None:
    disabled = settings.model_copy(update={"trading_enabled": False})
    with TestClient(create_app(disabled, mt5_adapter=adapter)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["details"]["trading_enabled"] is False


def test_console_logs_signal_post_and_file_events_without_secrets(
    settings, adapter, capsys, tmp_path
) -> None:
    signal = payload() | {"note": "log every execution detail"}
    app = create_app(settings, mt5_adapter=adapter)
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

    assert "signal_post" in events
    assert events["signal_post"]["state"] == "filled"
    assert events["signal_post"]["symbol"] == "EURUSD"
    assert "signal_received" not in events
    assert "signal_execution_completed" not in events

    events_path = settings.signals_log_path.parent / "events.jsonl"
    file_records = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    file_events = {record["event"]: record for record in file_records}
    assert file_events["signal_received"]["signal"]["note"] == "log every execution detail"
    assert file_events["mt5_order_send_completed"]["result"]["retcode"] == 10009

    signals_content = settings.signals_log_path.read_text(encoding="utf-8").strip()
    signal_record = json.loads(signals_content)
    assert signal_record["signal_id"] == signal["signal_id"]
    assert signal_record["state"] == "filled"

    assert settings.api_key.get_secret_value() not in output
    assert settings.password.get_secret_value() not in output


@pytest.mark.parametrize(
    "path",
    ["/v1/market-data/tick?quote=EURUSD", "/v1/market-data/candles?quote=EURUSD"],
)
def test_market_data_is_no_longer_served_here(settings, adapter, path: str) -> None:
    """It moved to market-data-service. A 404 here, not a silent proxy, is the
    hard cutover: a consumer still pointed at this port fails loudly."""
    with TestClient(create_app(settings, mt5_adapter=adapter)) as client:
        response = client.get(path, headers={"X-API-Key": "test-api-key-at-least-16"})

    assert response.status_code == 404


def test_console_logs_none_preflight_diagnostics(settings, adapter, capsys) -> None:
    adapter.check_result = None
    app = create_app(settings, mt5_adapter=adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=payload(),
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
        assert response.status_code == 503

    output = capsys.readouterr().out
    events_path = settings.signals_log_path.parent / "events.jsonl"
    file_records = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    events = {record["event"]: record for record in file_records}
    failed = events["mt5_order_check_returned_none"]

    assert failed["last_error"] == [-1, "fake error"]
    assert failed["request_diagnostics"]["comment"] == {
        "value": failed["request"]["comment"],
        "character_length": 15,
        "utf8_byte_length": 15,
        "ascii": True,
        "type": "str",
    }
    assert failed["request_diagnostics"]["field_types"]["comment"] == "str"
    assert events["service_error_response"]["status_code"] == 503
    assert events["service_error_response"]["error"]["code"] == "mt5_preflight_unavailable"

    console_records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    console_events = {record["event"]: record for record in console_records}
    assert console_events["service_error_response"]["status_code"] == 503
    assert settings.api_key.get_secret_value() not in output
    assert settings.password.get_secret_value() not in output
