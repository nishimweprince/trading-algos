from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.meta_event_notifications import NotificationResult
from app.services.meta_events import META_MODEL_FEATURES, STOP_ATR, TARGET_ATR
from app.services.meta_retraining import evaluate_weekly_shadow
from app.services.meta_shadow_store import MetaShadowStore
from app.services.meta_shadow_worker import MetaShadowWorker, _resolution


def _event(signal="2026-08-01T10:00:00Z"):
    return {
        "event_id": "event-1",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "signal_ts": pd.Timestamp(signal),
        "side": 1,
        "primary_setup_id": "bull_engulfing",
        "setup_ids": ["bull_engulfing"],
        "confidence": 0.9,
        "state": "awaiting_entry",
        "ineligible_reason": None,
        "forward_evaluation_eligible": False,
        "calendar_coverage_ok": True,
        "calendar_manifest_sha256": "a" * 64,
        "causal_features_v1": {"shape_48": [0.0] * 48},
        "causal_features_v2": {"shape_48": [0.0] * 48, "high_impact_next_24h": 1},
        "signal_close": 100.0,
        "atr_at_signal": 2.0,
        "source_boundary": pd.Timestamp("2026-06-30T23:00:00Z"),
    }


def _candles(highs, lows, opens=None, closes=None):
    size = len(highs)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2026-08-01T10:00:00Z", periods=size, freq="h"),
            "open": opens or [100.0] * size,
            "high": highs,
            "low": lows,
            "close": closes or [100.0] * size,
            "volume": 0.0,
        }
    )


def test_live_ledger_is_idempotent_and_keeps_predictions_separate(tmp_path):
    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    assert store.insert_event(_event()) is True
    assert store.insert_event(_event()) is False
    assert store.insert_prediction(
        {
            "artifact_version": "v1",
            "event_id": "event-1",
            "meta_feature_version": 1,
            "probability": 0.6,
            "threshold": 0.5,
            "would_take": True,
        }
    )
    assert store.insert_prediction(
        {
            "artifact_version": "v2",
            "event_id": "event-1",
            "meta_feature_version": 2,
            "probability": 0.7,
            "threshold": 0.55,
            "would_take": True,
        }
    )
    event = store.event_by_signal(
        symbol="XAUUSD",
        timeframe="H1",
        signal_ts=datetime(2026, 8, 1, 10, tzinfo=UTC),
    )
    assert event is not None
    assert [row["meta_feature_version"] for row in event["predictions"]] == [1, 2]
    assert event["forward_evaluation_eligible"] is False


def test_live_resolution_enters_next_open_and_resolves_early():
    atr, entry = 2.0, 101.0
    target = entry + TARGET_ATR * atr
    frame = _candles(
        [100.0, target],
        [100.0, 100.0],
        opens=[100.0, entry],
        closes=[100.0, target],
    )
    result = _resolution(frame, 0, 1, atr, "XAUUSD")
    assert result["state"] == "resolved"
    assert result["entry_price"] == entry
    assert result["outcome"] == "win"
    assert result["gross_r"] == pytest.approx(1.5)
    assert result["bars_to_resolution"] == 1


def test_live_resolution_is_open_until_barrier_or_timeout():
    atr, entry = 2.0, 101.0
    partial = _candles(
        [100.0, 102.0],
        [100.0, 100.0],
        opens=[100.0, entry],
    )
    opened = _resolution(partial, 0, 1, atr, "XAUUSD")
    assert opened["state"] == "open"
    assert opened["stop_price"] == entry - STOP_ATR * atr

    complete = _candles(
        [100.0] * 25,
        [100.0] * 25,
        opens=[100.0, entry, *([100.0] * 23)],
        closes=[100.0] * 25,
    )
    timeout = _resolution(complete, 0, 1, atr, "XAUUSD")
    assert timeout["state"] == "resolved"
    assert timeout["outcome"] == "timeout"
    assert timeout["bars_to_resolution"] == 24


def test_live_ambiguous_bar_is_a_loss():
    atr, entry = 2.0, 101.0
    stop = entry - STOP_ATR * atr
    target = entry + TARGET_ATR * atr
    frame = _candles(
        [100.0, target],
        [100.0, stop],
        opens=[100.0, entry],
    )
    result = _resolution(frame, 0, 1, atr, "XAUUSD")
    assert result["outcome"] == "loss"
    assert result["ambiguous_bar"] is True
    assert result["gross_r"] == pytest.approx(-1.0)


def test_unresolved_and_ineligible_events_do_not_enter_training_rows(tmp_path):
    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    store.insert_event(_event())
    ineligible = _event("2026-08-02T10:00:00Z")
    ineligible["event_id"] = "event-2"
    ineligible["state"] = "ineligible"
    ineligible["ineligible_reason"] = "calendar_coverage_unavailable"
    ineligible["calendar_coverage_ok"] = False
    ineligible["causal_features_v2"] = None
    store.insert_event(ineligible)
    assert store.resolved_training_events() == []


def test_shadow_history_does_not_reveal_future_resolution():
    from app.routers.meta_model import _causal_event

    event = {
        **_event(),
        "state": "resolved",
        "resolved_at": "2026-08-02T12:00:00+00:00",
        "entry_ts": "2026-08-01T11:00:00+00:00",
        "exit_ts": "2026-08-01T15:00:00+00:00",
        "outcome": "win",
        "net_r_3": 1.4,
        "net_r_5": 1.3,
        "net_r_8": 1.2,
        "predictions": [],
    }
    before = _causal_event(event, datetime(2026, 8, 2, 11, tzinfo=UTC))
    assert before["outcome"] is None
    assert before["exit_ts"] is None
    assert before["net_r_8"] is None

    after = _causal_event(event, datetime(2026, 8, 2, 13, tzinfo=UTC))
    assert after["outcome"] == "win"
    assert after["net_r_8"] == 1.2


@pytest.mark.parametrize(
    ("mode", "coverage_ok", "expected_predictions", "expected_resolved", "expected_alerts"),
    [
        ("catchup", True, 2, 1, 0),
        ("forward", True, 2, 1, 1),
        ("uncovered", False, 0, 0, 0),
    ],
)
def test_worker_notification_and_forward_evidence_gates(
    tmp_path,
    monkeypatch,
    mode,
    coverage_ok,
    expected_predictions,
    expected_resolved,
    expected_alerts,
):
    import app.services.meta_shadow_worker as worker_module

    signal = pd.Timestamp("2026-08-03T10:00:00Z")
    candles = pd.DataFrame(
        {
            "ts": pd.date_range(signal, periods=25, freq="h"),
            "open": [100.0] * 25,
            "high": [100.0, 107.0, *([100.0] * 23)],
            "low": [100.0] * 25,
            "close": [100.0] * 25,
            "volume": [0.0] * 25,
        }
    )
    feature = {name: 0.0 for name in META_MODEL_FEATURES}
    feature.update(
        {
            "ts": signal,
            "close": 100.0,
            "atr_at_bar": 2.0,
            "data_quality_reliable": True,
            "context_reliable": True,
            "shape_48": [0.0] * 48,
            "bar_tags": json.dumps(
                {
                    "tags": [
                        {
                            "setup_id": "bull_engulfing",
                            "state": "complete",
                            "side": 1,
                            "source": "rule",
                            "confidence": 0.9,
                        }
                    ]
                }
            ),
        }
    )
    features = pd.DataFrame([feature])
    monkeypatch.setattr(worker_module, "_load_partitions", lambda *args: candles.copy())
    monkeypatch.setattr(worker_module, "_feature_rows", lambda *args: features.copy())
    monkeypatch.setattr(worker_module, "_spread_by_ts", lambda *args: {})
    monkeypatch.setattr(
        worker_module,
        "build_calendar_feature_frame",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "calendar_coverage_ok": coverage_ok,
                    "high_impact_next_24h": 1,
                    "mins_to_next_high_impact": 60.0,
                    "mins_since_last_high_impact": 60.0,
                    "in_pre_news_window": True,
                    "in_post_news_window": True,
                    "high_impact_count_today": 2,
                }
            ]
        ),
    )
    manifest = tmp_path / "calendar-manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(worker_module, "calendar_manifest_path", lambda: manifest)
    monkeypatch.setattr(
        worker_module,
        "read_active_shadow",
        lambda: {"active_version": "v1", "challenger_version": "v2"},
    )

    class DummyModel:
        def predict_proba(self, frame):
            return [0.6] * len(frame)

    def artifact(version):
        columns = tuple(META_MODEL_FEATURES)
        if version == "v2":
            columns = (*columns, *worker_module.CALENDAR_MODEL_FEATURES)
        return DummyModel(), {
            "meta_feature_version": 1 if version == "v1" else 2,
            "feature_columns": list(columns),
            "threshold": 0.5,
            "orders_enabled": False,
        }

    monkeypatch.setattr(worker_module, "load_meta_artifact", artifact)

    class FakeSync:
        def sync(self, **kwargs):
            return SimpleNamespace(
                published=25,
                unexpected_gaps=0,
                histdata_cutoff=(signal - pd.Timedelta(hours=1)).to_pydatetime(),
            )

    class SpyNotifier:
        def __init__(self):
            self.calls = []

        def notify(self, event, predictions):
            persisted = store.event_by_signal(
                symbol="XAUUSD",
                timeframe="H1",
                signal_ts=signal.to_pydatetime(),
            )
            assert persisted is not None
            assert len(persisted["predictions"]) == len(predictions)
            self.calls.append((event, predictions))
            return NotificationResult("sent", "request-1")

    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    if mode != "catchup":
        store.set_state(
            "forward_shadow_start_ts",
            (signal - pd.Timedelta(hours=1)).isoformat(),
        )
    notifier = SpyNotifier()
    worker = MetaShadowWorker(sync=FakeSync(), store=store, epic="GOLD", notifier=notifier)
    first = worker.run_once()
    assert first["inserted_events"] == 1
    assert first["inserted_predictions"] == expected_predictions
    assert first["resolved"] == expected_resolved
    assert len(notifier.calls) == expected_alerts
    assert first["notifications"]["attempted"] == expected_alerts
    assert first["notifications"]["sent"] == expected_alerts
    event = store.event_by_signal(
        symbol="XAUUSD",
        timeframe="H1",
        signal_ts=signal.to_pydatetime(),
    )
    assert event is not None
    assert event["forward_evaluation_eligible"] is (mode != "catchup")
    assert event["state"] == ("ineligible" if mode == "uncovered" else "resolved")
    assert event["ineligible_reason"] == (
        "calendar_coverage_unavailable" if mode == "uncovered" else None
    )

    second = worker.run_once()
    assert second["inserted_events"] == 0
    assert second["inserted_predictions"] == 0
    assert len(notifier.calls) == expected_alerts


def test_weekly_evaluator_is_schedule_and_snapshot_idempotent(tmp_path, monkeypatch):
    import app.services.meta_retraining as retraining

    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    friday = evaluate_weekly_shadow(store, now=datetime(2026, 8, 7, 12, tzinfo=UTC))
    assert friday["status"] == "not_due"

    pointer = {
        "active_version": "v1",
        "challenger_version": "v2",
        "activated_at": "2026-08-01T00:00:00+00:00",
    }
    monkeypatch.setattr(retraining, "read_active_shadow", lambda: pointer)
    monkeypatch.setattr(
        retraining,
        "create_training_snapshot",
        lambda store, cutoff: {"sha256": "same"},
    )
    store.set_state("last_training_snapshot_sha256", "same")
    saturday = evaluate_weekly_shadow(store, now=datetime(2026, 8, 8, 12, tzinfo=UTC))
    assert saturday["status"] == "no_change"


def test_weekly_evaluator_keeps_challenger_until_forward_minimum(tmp_path, monkeypatch):
    import app.services.meta_retraining as retraining

    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    monkeypatch.setattr(
        retraining,
        "read_active_shadow",
        lambda: {
            "active_version": "v1",
            "challenger_version": "v2",
            "activated_at": "2026-08-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        retraining,
        "create_training_snapshot",
        lambda store, cutoff: {"sha256": "new"},
    )
    report = evaluate_weekly_shadow(store, now=datetime(2026, 8, 8, 12, tzinfo=UTC))
    assert report["status"] == "insufficient_forward_evidence"
    assert report["resolved_forward_events"] == 0
    assert report["required"] == 250
    assert store.state("last_training_snapshot_sha256") == "new"

    repeated = evaluate_weekly_shadow(store, now=datetime(2026, 8, 15, 12, tzinfo=UTC))
    assert repeated["status"] == "no_change"
