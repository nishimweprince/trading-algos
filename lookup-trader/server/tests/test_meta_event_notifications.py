from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.services.meta_event_notifications import MetaEventNotifier


def _client(handler) -> httpx.Client:
    """A MetaEventNotifier wired to a fake transport.

    These used to monkeypatch urllib's urlopen on the module. Delivery is now
    ta_notify.SyncNotifier over httpx, so the seam is an injected client rather
    than a patched global -- which also means the tests exercise the real
    request-building path instead of a stand-in for it.
    """
    return httpx.Client(transport=httpx.MockTransport(handler))


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


def test_notification_request_contract_and_message():
    captured = {}

    def send(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(201, json={"requestId": "request-1", "deliveryIds": ["delivery-1"]})

    result = _notifier(client=_client(send)).notify(_event(), _predictions())

    assert result.status == "sent"
    assert result.request_id == "request-1"
    request = captured["request"]
    assert str(request.url) == "http://127.0.0.1:3010/notifications"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer private-key"
    payload = json.loads(request.content)
    # Sorted, not insertion-ordered: ta_notify emits sorted(channels). The
    # service treats channels as a set, so this is a wire-order change only.
    assert payload["channels"] == ["EMAIL", "TELEGRAM"]
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
    assert "INDICATIVE LEVELS — SIGNAL-CLOSE ANCHOR" in payload["message"]
    assert "Reference: 3500.00" in payload["message"]
    assert "Stop loss: 3460.00 (2× ATR)" in payload["message"]
    assert "Take profit: 3560.00 (3× ATR)" in payload["message"]
    assert "Final entry, stop, and target reset from the next H1 open." in payload["message"]

    # An alert fires for every eligible forward event, not only for takes, so a
    # skip has to read as one. The base tagger decides when to alert; the model
    # score rides along as context.
    assert "Would take: YES" in payload["message"]


def test_notification_message_allows_one_available_artifact():
    captured = {}

    def send(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"requestId": "request-1"})

    result = _notifier(client=_client(send)).notify(_event(), _predictions()[:1])
    assert result.status == "sent"
    assert "Active artifact: v1" in captured["payload"]["message"]
    assert "v2" not in captured["payload"]["message"]


def test_notification_selects_active_artifact_instead_of_list_order():
    captured = {}

    def send(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"requestId": "request-1"})

    notifier = _notifier(client=_client(send))
    assert notifier.notify(_event(), list(reversed(_predictions()))).status == "sent"
    assert "Active artifact: v1" in captured["payload"]["message"]
    assert "v2" not in captured["payload"]["message"]


def test_short_notification_reflects_levels():
    captured = {}

    def send(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"requestId": "request-1"})

    event = {**_event(), "side": -1}
    assert _notifier(client=_client(send)).notify(event, _predictions()).status == "sent"
    message = captured["payload"]["message"]
    assert "Stop loss: 3540.00 (2× ATR)" in message
    assert "Take profit: 3440.00 (3× ATR)" in message


def test_skip_notification_does_not_present_trade_levels():
    captured = {}

    def send(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"requestId": "request-1"})

    predictions = [{**_predictions()[0], "probability": 0.49, "would_take": False}]
    assert _notifier(client=_client(send)).notify(_event(), predictions).status == "sent"
    assert "Would take: NO — SKIP" in captured["payload"]["message"]
    assert "INDICATIVE LEVELS" not in captured["payload"]["message"]


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"status": "skipped"}, "remote_skipped"),
        # Was "failed". ta_notify treats any 2xx as sent, because the service
        # also answers 202 + {"accepted": true} with no requestId -- which the
        # urllib code this replaces would have recorded as undelivered.
        (201, {"deliveryIds": []}, "sent"),
        (500, {"message": "error"}, "failed"),
    ],
)
def test_notification_response_contract(status, payload, expected):
    client = _client(lambda request: httpx.Response(status, json=payload))
    assert _notifier(client=client).notify(_event(), _predictions()).status == expected


def test_notification_failure_is_secret_safe_and_best_effort(caplog):
    """ta_notify logs the exception type, not its message, for exactly this reason."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise OSError("transport failed private-key")

    with caplog.at_level(logging.WARNING):
        result = _notifier(client=_client(fail)).notify(_event(), _predictions())
    assert result.status == "failed"
    assert "private-key" not in caplog.text
    # ta_core.log_event puts fields in the record's extras, not the message.
    assert caplog.records[-1].event_fields["error"] == "OSError"


def test_disabled_notifications_do_not_touch_the_network():
    def unreachable(request: httpx.Request) -> httpx.Response:
        pytest.fail("network should not be called")

    notifier = _notifier(
        enabled=False, base_url="", channels="", timeout_seconds=0, client=_client(unreachable)
    )
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
