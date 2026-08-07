"""The outcome dataset boundary must never admit forward information."""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from app.config import settings
from app.ml.outcome.features import CAUSAL_FEATURES, validate_causal_features
from app.services.export_bar_features import _schema_hash, export_bar_features, manifest_path


@pytest.mark.parametrize(
    "name",
    [
        "fwd24_max_atr",
        "fwd_shape",
        "level_touch",
        "next_open",
        "y",
        "y_outcome",
        "target",
        "target_atr",
    ],
)
def test_causal_allow_list_rejects_leakage_fields(name):
    with pytest.raises(ValueError):
        validate_causal_features([name])


def _feature_store(tmp_path, rows: int = 520):
    data = {
        "ts": pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC"),
        "feature_version": [settings.feature_version] * rows,
        "bar_feature_version": [settings.bar_feature_version] * rows,
        "bar_tags": [
            json.dumps(
                {
                    "version": settings.bar_feature_version,
                    "tags": [
                        {
                            "setup_id": "double_bottom",
                            "state": "complete" if i % 2 else "forming",
                            "confidence": 0.75,
                        }
                    ],
                }
            )
            for i in range(rows)
        ],
        "level_touch": [
            json.dumps(
                {
                    "1.0": {"up": 5, "down": 6},
                    "1.5": {"up": 3, "down": 4},
                }
            )
        ]
        * rows,
        "fwd24_complete": [True] * rows,
    }
    string_features = {
        "trend_state",
        "atr_bucket",
        "session",
        "rsi_band",
        "day_of_week",
        "ema_slope_bucket",
        "atr_change_bucket",
        "htf_trend_state",
        "htf_atr_bucket",
    }
    bool_features = {"context_reliable", "session_overlap"}
    for name in CAUSAL_FEATURES:
        if name == "shape_48":
            data[name] = [[0.0] * 48 for _ in range(rows)]
        elif name in string_features:
            data[name] = ["bucket"] * rows
        elif name in bool_features:
            data[name] = [True] * rows
        else:
            data[name] = [1.0] * rows
    data["fwd24_complete"][-1] = False
    data["context_reliable"][-2] = False
    data["atr_at_signal"] = [1.0] * rows
    frame = pd.DataFrame(data)
    path = (
        tmp_path
        / "features"
        / "symbol=XAUUSD"
        / "timeframe=H1"
        / "year=2024"
        / "month=01"
        / "part-000.parquet"
    )
    path.parent.mkdir(parents=True)
    frame.to_parquet(path, index=False)


def test_export_is_complete_both_sides_and_manifest_is_deterministic(tmp_path, monkeypatch):
    _feature_store(tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"

    with duckdb.connect(":memory:") as con:
        count = export_bar_features(con, first, symbol="XAUUSD", timeframe="H1")
        export_bar_features(con, second, symbol="XAUUSD", timeframe="H1")

    exported = pd.read_parquet(first)
    assert count == len(exported)
    assert set(exported["side"]) == {-1, 1}
    assert exported.groupby("ts")["side"].apply(set).eq({-1, 1}).all()
    assert set(exported["y_outcome"]) <= {"win", "loss", "timeout"}
    assert exported["context_reliable"].all()
    assert not any(column.startswith("fwd") for column in exported.columns)
    assert {"level_touch", "next_open", "fwd_shape"}.isdisjoint(exported.columns)
    assert set(exported["dataset_partition"]) == {"development", "holdout"}
    assert (
        manifest_path(first).read_bytes() == manifest_path(second).read_bytes()
    )

    manifest = json.loads(manifest_path(first).read_text())
    assert manifest["schema_sha256"] == _schema_hash(exported)
    assert manifest["split_policy"]["purge_bars"] >= 48
    assert manifest["split_policy"]["embargo_bars"] >= 48
    assert manifest["timestamp_ranges"]["development"]["max"] < (
        manifest["timestamp_ranges"]["holdout"]["min"]
    )
