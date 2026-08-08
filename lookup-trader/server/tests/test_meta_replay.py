from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from app.services.calendar.features import CALENDAR_MODEL_FEATURES


def test_replay_uses_paired_causal_meta_artifacts(monkeypatch):
    import app.routers.meta_model as router

    class Result:
        def df(self):
            return pd.DataFrame(
                [
                    {
                        "ts": pd.Timestamp("2026-08-07T21:00:00Z"),
                        "data_quality_reliable": True,
                        "context_reliable": True,
                    }
                ]
            )

    class Connection:
        def execute(self, sql, params):
            assert "bar_features" in sql
            assert params[0:2] == ["XAUUSD", "H1"]
            return Result()

    calendar = {
        "calendar_coverage_ok": True,
        "high_impact_next_24h": 1,
        "mins_to_next_high_impact": 60.0,
        "mins_since_last_high_impact": 180.0,
        "in_pre_news_window": True,
        "in_post_news_window": False,
        "high_impact_count_today": 2,
    }
    monkeypatch.setattr(router, "_canonical_features", lambda row, side: {"causal": side})
    monkeypatch.setattr(
        router,
        "build_calendar_feature_frame",
        lambda *args, **kwargs: pd.DataFrame([calendar]),
    )
    monkeypatch.setattr(
        router,
        "read_active_shadow",
        lambda: {"active_version": "v1", "challenger_version": "v2"},
    )

    class Model:
        def __init__(self, probability):
            self.probability = probability

        def predict_proba(self, frame):
            assert len(frame) == 1
            return [self.probability]

    def load(version):
        feature_version = 1 if version == "v1" else 2
        columns = ["causal"]
        if feature_version == 2:
            columns.extend(CALENDAR_MODEL_FEATURES)
        return Model(0.61 if feature_version == 1 else 0.49), {
            "orders_enabled": False,
            "meta_feature_version": feature_version,
            "feature_columns": columns,
            "threshold": 0.55,
            "target_take_rate": 0.2,
        }

    monkeypatch.setattr(router, "load_meta_artifact", load)
    result = router.get_meta_replay(
        symbol="xauusd",
        timeframe="h1",
        signal_ts=datetime(2026, 8, 7, 21, tzinfo=UTC),
        side=1,
        con=Connection(),
    )

    assert result["orders_enabled"] is False
    assert [row["artifact_version"] for row in result["predictions"]] == ["v1", "v2"]
    assert [row["role"] for row in result["predictions"]] == ["active", "challenger"]
    assert [row["would_take"] for row in result["predictions"]] == [True, False]
    assert all(row["target_take_rate"] == 0.2 for row in result["predictions"])
    assert not {"outcome", "y_meta", "net_r_3"} & set(result)
