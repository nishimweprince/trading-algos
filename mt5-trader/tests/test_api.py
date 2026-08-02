from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from mt5_signal_service.api import create_app
from mt5_signal_service.mt5_adapter import ConnectionSnapshot


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


def test_console_logs_signal_post_and_file_events_without_secrets(
    settings, adapter, capsys, tmp_path
) -> None:
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


def test_tick_requires_api_key(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get("/v1/market-data/tick", params={"quote": "EURUSD"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_tick_returns_current_bid_ask(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/tick",
            params={"quote": "EURUSD"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 200
    assert response.json() == {
        "symbol": "EURUSD",
        "bid": adapter.tick.bid,
        "ask": adapter.tick.ask,
    }


def test_candles_requires_api_key(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get("/v1/market-data/candles", params={"quote": "EURUSD"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_candles_returns_expected_shape_with_defaults(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "EURUSD"
    assert body["timeframe"] == "M1"
    assert [set(c.keys()) for c in body["candles"]] == [
        {"time", "open", "high", "low", "close", "volume"}
    ] * len(body["candles"])
    assert adapter.copy_rates_calls[-1] == ("EURUSD", adapter.constants.timeframes["M1"], 500)


def test_candles_respects_quote_and_count_params(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD", "count": 120, "timeframe": "H1"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 200
    assert response.json()["timeframe"] == "H1"
    assert adapter.copy_rates_calls[-1] == ("EURUSD", adapter.constants.timeframes["H1"], 120)


def test_candles_rejects_symbol_not_allowed(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "USDJPY"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "symbol_not_allowed"


def test_candles_rejects_unknown_timeframe(settings, adapter) -> None:
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD", "timeframe": "XYZ"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_candles_rejects_count_over_cap(settings, adapter) -> None:
    capped = settings.model_copy(update={"max_candles_lookback": 10})
    with TestClient(create_app(capped, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD", "count": 50},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "count_exceeds_limit"


def test_candles_reports_terminal_not_ready(settings, adapter) -> None:
    adapter.connection = ConnectionSnapshot(False, None, False, False)
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "terminal_not_ready"


def test_candles_reports_candles_unavailable(settings, adapter) -> None:
    adapter.rates = None
    with TestClient(create_app(settings, adapter)) as client:
        response = client.get(
            "/v1/market-data/candles",
            params={"quote": "EURUSD"},
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "candles_unavailable"


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
        "character_length": 15,
        "utf8_byte_length": 15,
        "ascii": True,
        "type": "str",
    }
    assert failed["request_diagnostics"]["field_types"]["comment"] == "str"
    assert events["service_error_response"]["status_code"] == 503
    assert events["service_error_response"]["error"]["code"] == "mt5_preflight_unavailable"
    assert settings.api_key.get_secret_value() not in output
    assert settings.password.get_secret_value() not in output
