"""Labelled training rows: feature samples + triple-barrier labels + OFI PCA.

Rows are the replay's feature samples (``replay.py features``), grid samples
only: that is the cadence the live bridge decides on. Nothing here computes a
feature; the state engine did, through the live code path.

Labels (plan §1.4): from each sample's mid ``m``, walk the following grid mids
up to the horizon. The first to reach ``m (1 + b)`` labels ``up``, the first
to reach ``m (1 - b)`` labels ``down``; neither by the horizon is ``none``.
A path broken by a book reset or a recording gap (a missing mid, or grid steps
more than two apart) before a barrier is touched has no label.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from ofi_scalper_service.state_engine import LEVELS, OFI_WINDOWS_MS, feature_names

__all__ = [
    "CLASSES",
    "FEATURES",
    "HORIZONS_S",
    "add_labels",
    "apply_pca",
    "fit_pca",
    "label_column",
    "load_days",
    "triple_barrier",
]

HORIZONS_S = (1, 5, 10, 30)
CLASSES = ("down", "none", "up")  # label codes 0, 1, 2
FEATURES = feature_names(cross=True)
GRID_NS = 100_000_000
PCA_WINDOW_MS = 1000


def label_column(horizon_s: float) -> str:
    return f"label_{horizon_s:g}s"


def load_days(features_dir: Path, days: Iterable[str]) -> pd.DataFrame:
    """Grid rows of the given days (``YYYY-MM-DD``), ordered by symbol and time."""
    frames = []
    for day in days:
        parquet = features_dir / f"{day}.parquet"
        jsonl = features_dir / f"{day}.jsonl.gz"
        if parquet.is_file():
            frame = pd.read_parquet(parquet)
        elif jsonl.is_file():
            with gzip.open(jsonl, "rt", encoding="utf-8") as handle:
                frame = pd.DataFrame([json.loads(line) for line in handle])
        else:
            raise FileNotFoundError(f"no features for {day} in {features_dir}")
        frame["day"] = day
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["trigger"] == "grid"]
    return df.sort_values(["symbol", "t_ns"], kind="stable").reset_index(drop=True)


def triple_barrier(
    t_ns: np.ndarray,
    mid: np.ndarray,
    *,
    horizon_s: float,
    barrier_bp: float,
    grid_ns: int = GRID_NS,
) -> np.ndarray:
    """Labels for one symbol's consecutive grid samples: 0 down, 1 none, 2 up, NaN."""
    n = len(mid)
    mid = np.asarray(mid, dtype=float)
    t_ns = np.asarray(t_ns, dtype=np.int64)
    steps = int(round(horizon_s * 1e9 / grid_ns))
    b = barrier_bp / 10_000
    up_at, down_at = mid * (1 + b), mid * (1 - b)
    result = np.ones(n)
    decided = np.zeros(n, dtype=bool)
    broken = ~np.isfinite(mid)
    # A step between consecutive samples longer than two grid steps is a gap.
    step_gap = np.zeros(n, dtype=bool)
    step_gap[1:] = np.diff(t_ns) > 2 * grid_ns
    for k in range(1, steps + 1):
        future = np.full(n, np.nan)
        gap = np.ones(n, dtype=bool)
        if k < n:
            future[: n - k] = mid[k:]
            gap[: n - k] = step_gap[k:]
        broken |= ~decided & (gap | ~np.isfinite(future))
        live = ~decided & ~broken
        hit_up = live & (future >= up_at)
        hit_down = live & (future <= down_at)
        result[hit_up] = 2
        result[hit_down & ~hit_up] = 0
        decided |= hit_up | hit_down
    labels = np.where(decided | ~broken, result, np.nan)
    labels[~np.isfinite(mid)] = np.nan
    return labels


def add_labels(
    df: pd.DataFrame,
    horizons_s: Sequence[float] = HORIZONS_S,
    *,
    barrier_bp: float,
    grid_ns: int = GRID_NS,
) -> pd.DataFrame:
    """Add ``label_<h>s`` columns, computed per symbol on its own sample path."""
    out = df.copy()
    for horizon in horizons_s:
        column = np.full(len(out), np.nan)
        for _, index in out.groupby("symbol", sort=False).indices.items():
            column[index] = triple_barrier(
                out["t_ns"].to_numpy()[index],
                out["mid"].to_numpy(dtype=float)[index],
                horizon_s=horizon,
                barrier_bp=barrier_bp,
                grid_ns=grid_ns,
            )
        out[label_column(horizon)] = column
    return out


def _level_columns(window_ms: int) -> list[str]:
    return [f"ofi_l{level}_{window_ms}ms" for level in range(1, LEVELS + 1)]


def fit_pca(df: pd.DataFrame, window_ms: int = PCA_WINDOW_MS) -> tuple[float, ...]:
    """Weights for integrated multi-level OFI, from training rows only.

    The first principal component of the standardised level OFIs, mapped back
    to raw units (so ``sum(w_i * ofi_l_i)`` is what the engine computes), signed
    so the weights sum positive, scaled to absolute sum 1.
    """
    x = df[_level_columns(window_ms)].dropna().to_numpy(dtype=float)
    if len(x) < LEVELS + 2:
        return (1.0,) + (0.0,) * (LEVELS - 1)
    std = x.std(axis=0)
    std[std == 0] = 1.0
    z = (x - x.mean(axis=0)) / std
    _, vectors = np.linalg.eigh(np.cov(z, rowvar=False))
    raw = vectors[:, -1] / std
    if raw.sum() < 0:
        raw = -raw
    raw = raw / np.abs(raw).sum()
    return tuple(float(w) for w in raw)


def apply_pca(df: pd.DataFrame, weights: Sequence[float]) -> pd.DataFrame:
    """Recompute every ``ofi_int_<w>ms`` as the live engine does with these weights."""
    out = df.copy()
    w = np.asarray(weights, dtype=float)
    for window in OFI_WINDOWS_MS:
        levels = out[_level_columns(window)].to_numpy(dtype=float)
        out[f"ofi_int_{window}ms"] = levels @ w
    return out
