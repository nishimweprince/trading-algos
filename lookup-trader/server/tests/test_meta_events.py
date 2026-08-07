from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.ml.outcome.features import CAUSAL_FEATURES
from app.services.meta_events import (
    META_MODEL_FEATURES,
    REWARD_R,
    STOP_ATR,
    TARGET_ATR,
    _event_id,
    _resolve_label,
    _shape_for_side,
)
from app.services.pips import pip_size

client = TestClient(app)


def test_absolute_price_and_zero_volume_fields_do_not_cross_model_boundary():
    forbidden = {"close", "ema_value", "atr_at_signal", "atr_at_bar", "volume_z"}
    assert forbidden.isdisjoint(CAUSAL_FEATURES)
    assert forbidden.isdisjoint(META_MODEL_FEATURES)


def _candles(highs, lows, opens=None, closes=None):
    size = len(highs)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=size, freq="h", tz="UTC"),
            "open": opens or [100.0] * size,
            "high": highs,
            "low": lows,
            "close": closes or [100.0] * size,
            "volume": 0.0,
        }
    )


def test_label_uses_next_open_and_conservative_same_bar_loss():
    """Barriers are derived from the contract, not hardcoded.

    An earlier version wrote the levels out by hand, so widening the stop made
    the entry bar stop straddling both barriers and the test silently stopped
    exercising the ambiguous case it was named for.
    """
    atr, entry = 2.0, 101.0
    stop = entry - STOP_ATR * atr
    target = entry + TARGET_ATR * atr
    frame = _candles(
        [entry, target, *([entry] * 23)],
        [99.0, stop, *([99.0] * 23)],
        opens=[100.0, entry, *([100.0] * 23)],
    )
    result = _resolve_label(frame, 0, 1, atr, "XAUUSD")
    assert result is not None
    assert result["entry_price"] == entry
    assert result["stop_price"] == stop
    assert result["target_price"] == target
    assert result["outcome"] == "loss"
    assert result["ambiguous_bar"] is True
    assert result["gross_r"] == -1.0

    cost = 3.0 * pip_size("XAUUSD") / (atr * STOP_ATR)
    assert result["cost_r_3"] == pytest.approx(cost)
    assert result["net_r_3"] == pytest.approx(-1.0 - cost)
    assert result["y_meta"] == 0


def test_returns_are_in_r_not_atr():
    """A win is `REWARD_R`, a loss is exactly -1, whatever the stop width.

    While the stop was 1 ATR the two scales coincided, so nothing caught the
    difference. At a 2 ATR stop, dividing by ATR would report a loss as -2.0 and
    make expectancy incomparable between contracts.
    """
    atr, entry = 2.0, 100.0
    target = entry + TARGET_ATR * atr
    frame = _candles(
        [entry, target, *([entry] * 23)],
        [entry, entry, *([entry] * 23)],
        opens=[entry] * 25,
    )
    win = _resolve_label(frame, 0, 1, atr, "XAUUSD")

    assert win["outcome"] == "win"
    assert win["gross_r"] == pytest.approx(REWARD_R) == pytest.approx(1.5)
    # Cost is diluted by the stop distance, so it is half what a 1 ATR stop paid.
    assert win["cost_r_3"] == pytest.approx(3.0 * pip_size("XAUUSD") / (atr * STOP_ATR))


def test_short_shape_reflection_swaps_high_and_low():
    assert _shape_for_side([1.0, 3.0, -2.0, 0.5], -1) == [-1.0, 2.0, -3.0, -0.5]


def _write_review_fixture() -> str:
    event_id = _event_id("XAUUSD", "H1", "2026-01-13T13:00:00Z", 1)
    row = {
        "event_id": event_id,
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "signal_ts": pd.Timestamp("2026-01-13T13:00:00Z"),
        "side": 1,
        "primary_setup_id": "bull_engulfing",
        "setup_ids": json.dumps(["bull_engulfing"]),
        "confidence": 0.9,
        "tag_source": "rule",
        "tagger_model_version": None,
        "data_quality_reliable": True,
        "context_reliable": True,
        "bar_feature_version": settings.bar_feature_version,
        "meta_feature_version": 1,
        "meta_label_version": 1,
        "signal_close": 4320.0,
        "atr_at_signal": 10.0,
        "entry_ts": pd.Timestamp("2026-01-13T14:00:00Z"),
        "exit_ts": pd.Timestamp("2026-01-13T16:00:00Z"),
        "entry_price": 4321.0,
        "stop_price": 4311.0,
        "target_price": 4336.0,
        "exit_price": 4336.0,
        "outcome": "win",
        "gross_r": 1.5,
        "bars_to_resolution": 3,
        "ambiguous_bar": False,
        "y_meta": 1,
    }
    for feature in META_MODEL_FEATURES:
        row[feature] = [0.0] * 48 if feature == "shape_48" else 0.0
    for pips, cost in ((3, 0.03), (5, 0.05), (8, 0.08)):
        row[f"cost_r_{pips}"] = cost
        row[f"net_r_{pips}"] = 1.5 - cost
        row[f"y_meta_{pips}"] = 1
    path = settings.data_dir / "exports" / "meta_events_v1.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_parquet(path, index=False)
    return event_id


def test_review_api_hides_outcome_until_review_and_reveal():
    event_id = _write_review_fixture()
    listing = client.get("/meta-events", params={"symbol": "XAUUSD", "timeframe": "H1"})
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert "outcome" not in item
    assert "net_r_3" not in item

    detail = client.get(f"/meta-events/{event_id}")
    assert detail.status_code == 200
    assert "outcome" not in detail.json()
    assert "forward_candles" not in detail.json()

    blocked = client.post(f"/meta-events/{event_id}/reveal")
    assert blocked.status_code == 409
    reviewed = client.post(
        f"/meta-events/{event_id}/review",
        json={"verdict": "valid", "notes": "clean geometry", "phase": "pre"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["verdict"] == "valid"

    revealed = client.post(f"/meta-events/{event_id}/reveal")
    assert revealed.status_code == 200
    assert revealed.json()["outcome"] == "win"
    assert len(revealed.json()["forward_candles"]) == 24


def test_candle_pages_have_absolute_boundaries_without_duplicates():
    query = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "date_from": "2026-01-01T00:00:00Z",
        "date_to": "2026-12-31T23:59:59Z",
        "limit": 200,
    }
    first = client.get("/candles/page", params={**query, "offset": 0}).json()
    second = client.get("/candles/page", params={**query, "offset": 200}).json()
    assert first["total"] == second["total"] == 420
    assert len(first["items"]) == len(second["items"]) == 200
    assert first["items"][-1]["ts"] < second["items"][0]["ts"]
    assert set(item["ts"] for item in first["items"]).isdisjoint(
        item["ts"] for item in second["items"]
    )
