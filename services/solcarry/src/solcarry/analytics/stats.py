"""Small statistics helpers: quantiles, share negative, run lengths, drawdown durations."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def quantiles(s: pd.Series, qs: Iterable[float]) -> dict[str, float]:
    x = pd.Series(s).dropna()
    out: dict[str, float] = {}
    for q in qs:
        out[f"q{q:g}"] = float(x.quantile(q)) if len(x) else float("nan")
    return out


def describe(s: pd.Series) -> dict[str, float]:
    x = pd.Series(s).dropna()
    if x.empty:
        nan = float("nan")
        return {
            "n": 0,
            "mean": nan,
            "median": nan,
            "p5": nan,
            "p25": nan,
            "p75": nan,
            "p95": nan,
            "min": nan,
            "max": nan,
        }
    return {
        "n": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "p5": float(x.quantile(0.05)),
        "p25": float(x.quantile(0.25)),
        "p75": float(x.quantile(0.75)),
        "p95": float(x.quantile(0.95)),
        "min": float(x.min()),
        "max": float(x.max()),
    }


def share_negative(s: pd.Series) -> float:
    x = pd.Series(s).dropna()
    return float((x < 0).mean()) if len(x) else float("nan")


def longest_true_run(mask: pd.Series | np.ndarray) -> int:
    """Length (in samples) of the longest run of True values."""
    arr = np.asarray(mask, dtype=bool)
    if arr.size == 0 or not arr.any():
        return 0
    padded = np.concatenate([[False], arr, [False]])
    d = np.diff(padded.astype(int))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return int(np.max(ends - starts))


def longest_negative_run_days(f_ann_hourly: pd.Series, roll_days: int = 7) -> float:
    """Longest run (days) with the trailing ``roll_days`` mean funding below zero."""
    s = f_ann_hourly.dropna()
    if s.empty:
        return 0.0
    roll = s.rolling(f"{roll_days}D", min_periods=24).mean()
    return longest_true_run((roll < 0).to_numpy()) / 24.0


def longest_underwater_days(x_hourly_annual: pd.Series) -> float:
    """Longest stretch (days) where cumulative excess carry sits below its running peak.

    Input is an hourly series of annualized excess returns; cumulative carry is
    ``cumsum(x / 8760)`` starting from 0 (the initial peak).
    """
    x = x_hourly_annual.dropna()
    if x.empty:
        return 0.0
    cum = np.concatenate([[0.0], np.cumsum(x.to_numpy() / 8760.0)])
    peak = np.maximum.accumulate(cum)
    under = cum < peak - 1e-15
    return longest_true_run(under[1:]) / 24.0
