from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.services.calendar.features import (
    CALENDAR_CAUSAL_COLUMNS,
    DISTANCE_CAP_MINUTES,
    META_PREVIEW_COLUMNS,
    _features_for_signal,
)


def _coverage(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def test_calendar_feature_boundaries_and_observed_horizon() -> None:
    signal = pd.Timestamp("2026-06-15T10:00:00Z")
    horizon = pd.Timestamp("2026-06-16T14:00:00Z")
    high = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-06-15T08:00:00Z",
                "2026-06-15T12:00:00Z",
                "2026-06-16T14:00:00Z",
                "2026-06-16T14:01:00Z",
            ],
            utc=True,
        )
    )
    result = _features_for_signal(
        signal,
        horizon,
        high,
        _coverage(date(2026, 6, 8), date(2026, 6, 22)),
    )
    assert result["calendar_coverage_ok"] is True
    assert result["high_impact_in_horizon"] == 2
    assert result["mins_to_next_high_impact"] == 120
    assert result["mins_since_last_high_impact"] == 120
    assert result["in_pre_news_window"] is True
    assert result["in_post_news_window"] is True


def test_calendar_features_cap_distances_and_fail_closed_at_edges() -> None:
    signal = pd.Timestamp("2026-06-15T10:00:00Z")
    horizon = pd.Timestamp("2026-06-16T10:00:00Z")
    covered = _coverage(date(2026, 6, 8), date(2026, 6, 22))
    result = _features_for_signal(signal, horizon, pd.DatetimeIndex([]), covered)
    assert result["mins_to_next_high_impact"] == DISTANCE_CAP_MINUTES
    assert result["mins_since_last_high_impact"] == DISTANCE_CAP_MINUTES
    assert result["in_pre_news_window"] is False
    assert result["in_post_news_window"] is False

    unreliable = _features_for_signal(signal, horizon, pd.DatetimeIndex([]), {signal.date()})
    assert unreliable["calendar_coverage_ok"] is False
    assert unreliable["mins_to_next_high_impact"] is None


def test_preview_column_contract_is_causal() -> None:
    assert META_PREVIEW_COLUMNS == ("event_id", "signal_ts")
    forbidden = {"actual", "forecast", "previous", "revision", "y_meta", "net_r_3"}
    assert forbidden.isdisjoint(CALENDAR_CAUSAL_COLUMNS)
