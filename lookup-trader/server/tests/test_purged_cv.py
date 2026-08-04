"""Tests for purged cross-validation splits."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.purged_cv import assert_no_leakage, purged_kfold


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


def test_requires_minimum_rows():
    with pytest.raises(ValueError):
        purged_kfold(_hourly(3), horizon=6, n_splits=5)
