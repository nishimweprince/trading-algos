"""Tests for purged cross-validation splits."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.purged_cv import (
    assert_no_leakage,
    chronological_walk_forward,
    purged_kfold,
    timeframe_delta,
)


def _hourly(n: int, start: datetime | None = None) -> list[datetime]:
    base = start or datetime(2024, 1, 1)
    return [base + timedelta(hours=i) for i in range(n)]


def test_purged_folds_cover_all_test_indices():
    ts = _hourly(100)
    folds = purged_kfold(ts, horizon=24, n_splits=5)
    assert len(folds) == 5
    covered = sorted(int(i) for fold in folds for i in fold.test_idx)
    assert covered == list(range(100))


def test_no_leakage_across_folds():
    ts = _hourly(200)
    horizon = 24
    folds = purged_kfold(ts, horizon=horizon, n_splits=5, embargo_bars=horizon)
    for fold in folds:
        assert len(fold.train_idx) > 0
        assert len(fold.test_idx) > 0
    assert_no_leakage(ts, folds, horizon=horizon)


def test_short_requested_embargo_is_raised_to_store_horizon():
    ts = _hourly(300)
    folds = purged_kfold(ts, horizon=24, n_splits=5, embargo_bars=1)
    assert_no_leakage(ts, folds, horizon=48)


def test_requires_minimum_rows():
    with pytest.raises(ValueError):
        purged_kfold(_hourly(3), horizon=6, n_splits=5)


def test_walk_forward_is_chronological_and_keeps_holdout_untouched():
    ts = _hourly(500)
    plan = chronological_walk_forward(
        ts, timeframe="H1", horizon=24, n_splits=5, holdout_fraction=0.2
    )

    assert plan.purge_bars >= 48
    assert plan.embargo_bars >= 48
    assert plan.bar_delta == timedelta(hours=1)
    holdout_start = int(plan.holdout_idx[0])
    for fold in plan.folds:
        assert max(fold.train_idx) < min(fold.test_idx)
        assert max(fold.test_idx) < holdout_start
    assert_no_leakage(ts, list(plan.folds), horizon=48, bar_delta=plan.bar_delta)


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [("M15", timedelta(minutes=15)), ("H4", timedelta(hours=4)), ("D1", timedelta(days=1))],
)
def test_bar_delta_is_timeframe_aware(timeframe, expected):
    assert timeframe_delta(timeframe) == expected
