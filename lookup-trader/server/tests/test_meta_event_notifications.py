from __future__ import annotations

import json
import logging

import pytest

import app.services.meta_event_notifications as notification_module
from app.services.meta_event_notifications import MetaEventNotifier


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.body


def _event() -> dict:
    return {
        "event_id": "event-1",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "signal_ts": "2026-08-08T10:00:00+00:00",
        "side": 1,
        "primary_setup_id": "bull_engulfing",
        "setup_ids": ["bull_engulfing", "breakout"],
        "confidence": 0.9,
        "signal_close": 3500.0,
        "atr_at_signal": 20.0,
        "calendar_coverage_ok": True,
        "empirical_history": {
            "recommendation": {
                "verdict": "wait",
                "headline": "Wait",
                "rationale": "Estimated expectancy is not positive after assumed costs.",
            },
            "expectancy_r_net": -0.07,
            "net_expectancy_ci_low_r": -0.18,
            "net_expectancy_ci_high_r": 0.04,
            "win_rate": 0.386,
            "resolved_count": 1130,
            "independent_periods": 289,
            "fallback_used": True,
            "dropped_dimensions": ["session_overlap", "day_of_week"],
        },
    }


def _predictions() -> list[dict]:
    return [
        {
            "artifact_version": "v1",
            "meta_feature_version": 1,
            "probability": 0.61,
            "threshold": 0.55,
            "would_take": True,
            "role": "active",
        },
        {
            "artifact_version": "v2",
            "meta_feature_version": 2,
            "probability": 0.49,
            "threshold": 0.50,
            "would_take": False,
            "role": "challenger",
        },
    ]


def _notifier(**overrides) -> MetaEventNotifier:
    values = {
        "enabled": True,
        "base_url": "http://127.0.0.1:3010/",
        "api_key": "private-key",
        "channels": "telegram, EMAIL,telegram",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return MetaEventNotifier(**values)


def test_notification_request_contract_and_message(monkeypatch):
    captured = {}

    def send(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(201, {"requestId": "request-1", "deliveryIds": ["delivery-1"]})

    monkeypatch.setattr(notification_module, "urlopen", send)
    result = _notifier().notify(_event(), _predictions())

    assert result.status == "sent"
    assert result.request_id == "request-1"
    assert captured["timeout"] == 5
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:3010/notifications"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer private-key"
    payload = json.loads(request.data)
    assert payload["channels"] == ["TELEGRAM", "EMAIL"]
    assert payload["source"] == "lookup-trader.meta-shadow"
    assert payload["idempotencyKey"] == "meta-event:event-1"
    assert payload["contentType"] == "text"
    assert payload["subject"] == "XAUUSD H1 LONG meta event — bull_engulfing"
    assert "RESEARCH SHADOW — NO ORDER PLACED" in payload["message"]
    assert "EMPIRICAL HISTORY" in payload["message"]
    assert "Recommendation: WAIT" in payload["message"]
    assert "Estimated net: -0.07R" in payload["message"]
    assert "95% range: -0.18R to +0.04R" in payload["message"]
    assert "Win rate: 38.6% · 1130 resolved bars · 289 weeks" in payload["message"]
    assert "Context: Broader · session overlap + day of week dropped" in payload["message"]
    assert "MODEL RECOMMENDATION" in payload["message"]
    assert "Recommended direction: LONG" in payload["message"]
    assert "Would take: YES" in payload["message"]
    assert "Positive net outcome probability: 61.0%" in payload["message"]
    assert "Take threshold: 55.0%" in payload["message"]
    assert "Active artifact: v1" in payload["message"]
    assert "v2" not in payload["message"]

    # An alert fires for every eligible forward event, not only for takes, so a
    # skip has to read as one. The base tagger decides when to alert; the model
    # score rides along as context.
    assert "Would take: YES" in payload["message"]


def test_notification_message_allows_one_available_artifact(monkeypatch):
    captured = {}

    def send(request, timeout):
        captured["payload"] = json.loads(request.data)
        return _Response(201, {"requestId": "request-1"})

    monkeypatch.setattr(notification_module, "urlopen", send)
    result = _notifier().notify(_event(), _predictions()[:1])
    assert result.status == "sent"
    assert "Active artifact: v1" in captured["payload"]["message"]
    assert "v2" not in captured["payload"]["message"]


def test_notification_selects_active_artifact_instead_of_list_order(monkeypatch):
    captured = {}

    def send(request, timeout):
        captured["payload"] = json.loads(request.data)
        return _Response(201, {"requestId": "request-1"})

    monkeypatch.setattr(notification_module, "urlopen", send)
    assert _notifier().notify(_event(), list(reversed(_predictions()))).status == "sent"
    assert "Active artifact: v1" in captured["payload"]["message"]
    assert "v2" not in captured["payload"]["message"]


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"status": "skipped"}, "remote_skipped"),
        (201, {"deliveryIds": []}, "failed"),
        (500, {"message": "error"}, "failed"),
    ],
)
def test_notification_response_contract(monkeypatch, status, payload, expected):
    monkeypatch.setattr(
        notification_module,
        "urlopen",
        lambda request, timeout: _Response(status, payload),
    )
    assert _notifier().notify(_event(), _predictions()).status == expected


def test_notification_failure_is_secret_safe_and_best_effort(monkeypatch, caplog):
    def fail(request, timeout):
        raise OSError("transport failed private-key")

    monkeypatch.setattr(notification_module, "urlopen", fail)
    with caplog.at_level(logging.WARNING):
        result = _notifier().notify(_event(), _predictions())
    assert result.status == "failed"
    assert "private-key" not in caplog.text
    assert "OSError" in caplog.text


def test_disabled_notifications_do_not_touch_the_network(monkeypatch):
    monkeypatch.setattr(
        notification_module,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("network should not be called"),
    )
    notifier = _notifier(enabled=False, base_url="", channels="", timeout_seconds=0)
    assert notifier.notify(_event(), _predictions()).status == "disabled"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base_url": "localhost:3010"}, r"absolute HTTP\(S\)"),
        ({"channels": "TELEGRAM,PAGER"}, "Unsupported notification channels"),
        ({"channels": ""}, "At least one notification channel"),
        ({"timeout_seconds": 0}, "greater than zero"),
    ],
)
def test_enabled_notification_configuration_fails_closed(overrides, message):
    with pytest.raises(ValueError, match=message):
        _notifier(**overrides)
