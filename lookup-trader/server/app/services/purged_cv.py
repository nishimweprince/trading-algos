"""Combinatorial purged k-fold splits for bar-level time series.

Adjacent bars share forward windows, so random splits leak. This module drops
train rows whose [ts, ts + horizon] overlaps any test row and adds an embargo
gap after each test fold (de Prado purged CV).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np


@dataclass(frozen=True)
class PurgedFold:
    train_idx: np.ndarray
    test_idx: np.ndarray


def _interval_end(ts: datetime, horizon: int, bar_delta: timedelta) -> datetime:
  return ts + bar_delta * horizon


def purged_kfold(
    timestamps: list[datetime],
    *,
    horizon: int,
    n_splits: int = 5,
    embargo_bars: int | None = None,
    bar_delta: timedelta | None = None,
) -> list[PurgedFold]:
    """Return index arrays for purged combinatorial k-fold splits.

    `bar_delta` defaults to one hour — override for non-H1 series.
    """
    if len(timestamps) < n_splits:
        raise ValueError(f"Need at least {n_splits} rows for {n_splits} folds")

    embargo = embargo_bars if embargo_bars is not None else horizon
    delta = bar_delta or timedelta(hours=1)
    n = len(timestamps)
    fold_sizes = np.full(n_splits, n // n_splits, dtype=int)
    fold_sizes[: n % n_splits] += 1

    folds: list[PurgedFold] = []
    start = 0
    for fold_size in fold_sizes:
        test_idx = np.arange(start, start + fold_size)
        start += fold_size

        test_start = timestamps[int(test_idx[0])]
        test_end = _interval_end(timestamps[int(test_idx[-1])], horizon, delta)
        embargo_end = test_end + delta * embargo

        train_idx = []
        for i, ts in enumerate(timestamps):
            if i in test_idx:
                continue
            row_end = _interval_end(ts, horizon, delta)
            # Purge train rows whose forward window overlaps the test fold.
            if row_end > test_start and ts < test_end:
                continue
            # Embargo: drop train rows immediately after the test fold.
            if test_end <= ts < embargo_end:
                continue
            train_idx.append(i)

        folds.append(PurgedFold(train_idx=np.array(train_idx, dtype=int), test_idx=test_idx))

    return folds


def assert_no_leakage(
    timestamps: list[datetime],
    folds: list[PurgedFold],
    *,
    horizon: int,
    bar_delta: timedelta | None = None,
) -> None:
    """Raise AssertionError if any train row overlaps a test row's forward window."""
    delta = bar_delta or timedelta(hours=1)
    for fold in folds:
        for ti in fold.test_idx:
            test_start = timestamps[int(ti)]
            test_end = _interval_end(test_start, horizon, delta)
            for tri in fold.train_idx:
                ts = timestamps[int(tri)]
                row_end = _interval_end(ts, horizon, delta)
                if row_end > test_start and ts < test_end:
                    raise AssertionError(
                        f"Leakage: train idx {tri} overlaps test idx {ti}"
                    )
