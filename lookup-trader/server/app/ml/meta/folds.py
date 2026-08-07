"""Chronological folds for sparse meta-events.

`purged_cv.chronological_walk_forward` splits the row index into equal blocks
and purges by subtracting a bar count from an index position. That is right when
one row is one bar. Meta-events are sparse — mean spacing is about six hours —
so 48 index positions is roughly twelve days of wall clock, and block boundaries
fall in arbitrary places rather than on year ends.

Two changes here. Folds are calendar years, which the roadmap specifies and
which are interpretable when a fold looks odd. And purging uses each event's
real `[entry_ts, exit_ts]` interval instead of a blanket horizon: a training
event is dropped when the trade it describes was still open once the test block
began. That is the actual leak, and the events already carry the exact dates.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Wall-clock gap held after a test block before training resumes. Matches the
# `MIN_OUTCOME_GAP_BARS = 48` convention in `purged_cv`, expressed in hours
# because these events are H1.
EMBARGO = pd.Timedelta(hours=48)


@dataclass(frozen=True)
class YearFold:
    """One expanding-window fold, with the purge accounted for."""

    test_year: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    purged: int

    @property
    def label(self) -> str:
        return f"test {self.test_year}"


def year_folds(
    frame: pd.DataFrame,
    *,
    first_test_year: int,
    last_test_year: int,
    entry_col: str = "entry_ts",
    exit_col: str = "exit_ts",
) -> list[YearFold]:
    """Expanding folds: everything before year Y trains, year Y tests.

    Training is restricted to events that had already closed before the test
    year opened, plus the embargo. An event whose trade was still running into
    the test block shares price action with it, which is the overlap that makes
    naive splits leak.
    """
    entry = pd.to_datetime(frame[entry_col], utc=True)
    exit_ = pd.to_datetime(frame[exit_col], utc=True)
    positions = np.arange(len(frame))

    folds: list[YearFold] = []
    for year in range(first_test_year, last_test_year + 1):
        test_open = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
        test_close = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC")
        test_mask = (entry >= test_open) & (entry < test_close)
        if not test_mask.any():
            continue

        earlier = entry < test_open
        # The purge: an earlier event still open at the test boundary overlaps it.
        closed_in_time = exit_ <= (test_open - EMBARGO)
        train_mask = earlier & closed_in_time
        folds.append(
            YearFold(
                test_year=year,
                train_idx=positions[train_mask.to_numpy()],
                test_idx=positions[test_mask.to_numpy()],
                purged=int((earlier & ~closed_in_time).sum()),
            )
        )
    return folds


def audit_split(frame: pd.DataFrame, *, audit_from_year: int) -> tuple[np.ndarray, np.ndarray]:
    """Development and audit index arrays, separated by the same embargo."""
    entry = pd.to_datetime(frame["entry_ts"], utc=True)
    exit_ = pd.to_datetime(frame["exit_ts"], utc=True)
    boundary = pd.Timestamp(year=audit_from_year, month=1, day=1, tz="UTC")
    positions = np.arange(len(frame))

    audit = positions[(entry >= boundary).to_numpy()]
    development = positions[((entry < boundary) & (exit_ <= boundary - EMBARGO)).to_numpy()]
    return development, audit


def assert_no_overlap(frame: pd.DataFrame, folds: list[YearFold]) -> None:
    """Raise if any training trade was still open when its test block started.

    Vectorised deliberately. The equivalent check in `purged_cv` compares every
    train row against every test row in Python, which is fine for the few
    hundred rows its tests use and quadratic on twenty-five thousand events.
    """
    entry = pd.to_datetime(frame["entry_ts"], utc=True).to_numpy()
    exit_ = pd.to_datetime(frame["exit_ts"], utc=True).to_numpy()
    for fold in folds:
        if len(fold.train_idx) == 0 or len(fold.test_idx) == 0:
            continue
        test_open = entry[fold.test_idx].min()
        latest_train_exit = exit_[fold.train_idx].max()
        if latest_train_exit > test_open:
            raise AssertionError(
                f"Leakage in {fold.label}: a training trade closed at "
                f"{latest_train_exit} but the test block opened at {test_open}"
            )
