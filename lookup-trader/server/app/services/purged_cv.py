"""Combinatorial purged k-fold splits for bar-level time series.

Adjacent bars share forward windows, so random splits leak. This module drops
train rows whose [ts, ts + max(horizon, 48 bars)] overlaps any test row and adds
an embargo gap of at least the same size (de Prado purged CV).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

import numpy as np

MIN_OUTCOME_GAP_BARS = 48


@dataclass(frozen=True)
class PurgedFold:
    train_idx: np.ndarray
    test_idx: np.ndarray


@dataclass(frozen=True)
class WalkForwardPlan:
    """Expanding-window folds plus a final test set no fold may inspect."""

    folds: tuple[PurgedFold, ...]
    holdout_idx: np.ndarray
    purged_idx: np.ndarray
    purge_bars: int
    embargo_bars: int
    bar_delta: timedelta


def timeframe_delta(timeframe: str) -> timedelta:
    """Convert canonical M/H/D/W bar names to their wall-clock duration."""
    match = re.fullmatch(r"([MHDW])(\d+)", timeframe.upper())
    if not match:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    unit, raw_count = match.groups()
    count = int(raw_count)
    if count <= 0:
        raise ValueError("timeframe count must be positive")
    base = {
        "M": timedelta(minutes=1),
        "H": timedelta(hours=1),
        "D": timedelta(days=1),
        "W": timedelta(weeks=1),
    }[unit]
    return base * count


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

    `bar_delta` defaults to one hour — override for non-H1 series. Purge and
    embargo are never shorter than the feature store's 48-bar maximum horizon.
    """
    if len(timestamps) < n_splits:
        raise ValueError(f"Need at least {n_splits} rows for {n_splits} folds")

    effective_horizon = max(horizon, MIN_OUTCOME_GAP_BARS)
    embargo = max(
        embargo_bars if embargo_bars is not None else effective_horizon,
        effective_horizon,
    )
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
        test_end = _interval_end(timestamps[int(test_idx[-1])], effective_horizon, delta)
        embargo_end = test_end + delta * embargo

        train_idx = []
        for i, ts in enumerate(timestamps):
            if i in test_idx:
                continue
            row_end = _interval_end(ts, effective_horizon, delta)
            # Purge train rows whose forward window overlaps the test fold.
            if row_end > test_start and ts < test_end:
                continue
            # Embargo: drop train rows immediately after the test fold.
            if test_end <= ts < embargo_end:
                continue
            train_idx.append(i)

        folds.append(PurgedFold(train_idx=np.array(train_idx, dtype=int), test_idx=test_idx))

    return folds


def chronological_walk_forward(
    timestamps: list[datetime],
    *,
    timeframe: str,
    horizon: int,
    n_splits: int = 5,
    embargo_bars: int | None = None,
    holdout_fraction: float = 0.2,
    minimum_gap_bars: int = MIN_OUTCOME_GAP_BARS,
) -> WalkForwardPlan:
    """Build expanding chronological folds and an untouched terminal holdout.

    Every validation range is strictly later than its training range.  A gap of
    at least the largest stored outcome horizon separates them; the same gap
    separates all development data from the final holdout.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be positive")
    if not 0 < holdout_fraction < 1:
        raise ValueError("holdout_fraction must be between 0 and 1")
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise ValueError("timestamps must be unique and strictly chronological")

    purge = max(horizon, minimum_gap_bars)
    embargo = max(embargo_bars if embargo_bars is not None else purge, purge)
    gap = max(purge, embargo)
    n = len(timestamps)
    holdout_count = max(1, int(np.ceil(n * holdout_fraction)))
    holdout_start = n - holdout_count
    development_end = holdout_start - gap
    minimum_development = n_splits + 1
    if development_end < minimum_development:
        raise ValueError(
            "Not enough rows for chronological folds, the required gap, and holdout"
        )

    # Split development into one initial train block and n_splits validation
    # blocks. Training expands, while the immediately preceding gap is purged.
    blocks = np.array_split(np.arange(development_end), n_splits + 1)
    if any(len(block) == 0 for block in blocks):
        raise ValueError("Not enough development rows for requested folds")

    folds: list[PurgedFold] = []
    purged: set[int] = set(range(development_end, holdout_start))
    for test in blocks[1:]:
        test_start = int(test[0])
        train_end = max(0, test_start - gap)
        if train_end == 0:
            raise ValueError("Not enough pre-test rows after purge/embargo")
        train = np.arange(train_end, dtype=int)
        purged.update(range(train_end, test_start))
        folds.append(PurgedFold(train_idx=train, test_idx=test.astype(int)))

    return WalkForwardPlan(
        folds=tuple(folds),
        holdout_idx=np.arange(holdout_start, n, dtype=int),
        purged_idx=np.array(sorted(purged), dtype=int),
        purge_bars=purge,
        embargo_bars=embargo,
        bar_delta=timeframe_delta(timeframe),
    )


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
