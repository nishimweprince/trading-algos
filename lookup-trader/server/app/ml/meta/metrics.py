"""Scoring for a binary take/skip model.

`outcome/metrics.py::_expectancy` is deliberately not reused. It reconstructs
expectancy from a fixed 1.5R/-1.0R payoff and divides costs by a raw ATR, which
silently assumed a 1 ATR stop; under the 2 ATR contract that denominator is
wrong. The event export now carries exact realised `net_r_{3,5,8}` per event, so
expectancy is measured by sweeping a take threshold over those columns rather
than derived from class probabilities.

Every comparison here is a *lift over taking every event in the same block*.
Absolute net R is misleading across blocks: take-all earns +0.0335R over
2025-2026H1 and -0.0515R over 2009-2024, so a model can look profitable on the
audit block purely by being evaluated there.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

COST_COLUMNS = ("net_r_3", "net_r_5", "net_r_8")


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict:
    """Calibration of the positive-class probability.

    Not `outcome.metrics.reliability_data`, which indexes labels through the
    three-class `CLASS_ORDER` and raises on 0/1. It also bins by argmax
    confidence, which for a binary model collapses 0.2 and 0.8 into the same
    bucket; here a bin holds events predicted at a similar take probability and
    is compared against how often they actually paid.
    """
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows, ece = [], 0.0
    for index in range(bins):
        lower, upper = float(edges[index]), float(edges[index + 1])
        selected = (p >= lower) & (p <= upper if index == bins - 1 else p < upper)
        count = int(selected.sum())
        mean_p = float(p[selected].mean()) if count else None
        observed = float(y[selected].mean()) if count else None
        if count:
            ece += count / len(y) * abs(mean_p - observed)
        rows.append(
            {
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_predicted": mean_p,
                "observed_rate": observed,
            }
        )
    return {"ece": float(ece), "bins": rows}


def probability_scores(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    """Calibration and ranking quality, independent of any threshold."""
    out = {
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "brier": float(brier_score_loss(y, p)),
        "positive_rate": float(np.mean(y)),
        "mean_predicted": float(np.mean(p)),
    }
    # AUC is undefined on a single-class block; report null rather than crash.
    if len(np.unique(y)) > 1:
        out["auc"] = float(roc_auc_score(y, p))
        out["pr_auc"] = float(average_precision_score(y, p))
    else:
        out["auc"] = out["pr_auc"] = None
    out["ece"] = float(reliability(y, p)["ece"])
    return out


def take_all(frame: pd.DataFrame) -> dict[str, float]:
    """The floor: enter every event the base strategy produced."""
    return {
        "events": int(len(frame)),
        "taken": int(len(frame)),
        "take_rate": 1.0,
        **{f"{col}_per_event": float(frame[col].mean()) for col in COST_COLUMNS},
        "total_net_r_3": float(frame["net_r_3"].sum()),
        "win_rate": float(frame["y_meta"].mean()),
    }


def at_threshold(frame: pd.DataFrame, p: np.ndarray, threshold: float) -> dict[str, float]:
    """What taking only `p >= threshold` would have produced on this block."""
    taken = frame.loc[np.asarray(p) >= threshold]
    if taken.empty:
        return {
            "threshold": float(threshold),
            "events": int(len(frame)),
            "taken": 0,
            "take_rate": 0.0,
            **{f"{col}_per_event": None for col in COST_COLUMNS},
            "total_net_r_3": 0.0,
            "win_rate": None,
            "lift_vs_take_all": None,
        }
    return {
        "threshold": float(threshold),
        "events": int(len(frame)),
        "taken": int(len(taken)),
        "take_rate": float(len(taken) / len(frame)),
        **{f"{col}_per_event": float(taken[col].mean()) for col in COST_COLUMNS},
        "total_net_r_3": float(taken["net_r_3"].sum()),
        "win_rate": float(taken["y_meta"].mean()),
        # The only number that means anything across blocks.
        "lift_vs_take_all": float(taken["net_r_3"].mean() - frame["net_r_3"].mean()),
    }


def sweep(
    frame: pd.DataFrame,
    p: np.ndarray,
    *,
    min_take_rate: float = 0.05,
    steps: int = 91,
) -> list[dict[str, float]]:
    """Threshold curve, floored at a take rate worth trading.

    Without the floor the sweep always selects the extreme threshold, where a
    handful of events happen to have gone well and the mean is noise.
    """
    grid = np.linspace(0.05, 0.95, steps)
    rows = [at_threshold(frame, p, float(t)) for t in grid]
    return [row for row in rows if row["take_rate"] >= min_take_rate]


def choose_threshold(
    frame: pd.DataFrame, p: np.ndarray, *, min_take_rate: float = 0.05
) -> float:
    """Best out-of-fold threshold by net R. Must never see the audit block."""
    rows = sweep(frame, p, min_take_rate=min_take_rate)
    if not rows:
        return 0.5
    return max(rows, key=lambda row: row["net_r_3_per_event"])["threshold"]


def stability(frame: pd.DataFrame, p: np.ndarray, threshold: float) -> dict[str, dict]:
    """Does the edge survive slicing? A single good year or side is not an edge."""
    work = frame.copy()
    work["_take"] = np.asarray(p) >= threshold
    work["_year"] = pd.to_datetime(work["signal_ts"], utc=True).dt.year

    slices = (("by_year", "_year"), ("by_side", "side"), ("by_setup", "primary_setup_id"))
    out: dict[str, dict] = {}
    for key, column in slices:
        cells: dict[str, dict] = {}
        for value, group in work.groupby(column):
            taken = group.loc[group["_take"]]
            cells[str(value)] = {
                "events": int(len(group)),
                "taken": int(len(taken)),
                "net_r_3_per_event": float(taken["net_r_3"].mean()) if len(taken) else None,
                "lift_vs_take_all": (
                    float(taken["net_r_3"].mean() - group["net_r_3"].mean())
                    if len(taken)
                    else None
                ),
            }
        out[key] = cells
    return out


def block_bootstrap_ci(
    net_r: np.ndarray,
    *,
    block: int = 50,
    draws: int = 2000,
    seed: int = 0,
) -> dict[str, float]:
    """Confidence bound on mean net R that respects serial dependence.

    Events overlap in time, so an i.i.d. bootstrap understates the interval.
    Resampling contiguous blocks keeps neighbouring trades together.
    """
    values = np.asarray(net_r, dtype=float)
    if len(values) < block * 2:
        return {"mean": float(values.mean()) if len(values) else 0.0, "lo": None, "hi": None}
    rng = np.random.default_rng(seed)
    starts_max = len(values) - block
    n_blocks = int(np.ceil(len(values) / block))
    means = np.empty(draws)
    for i in range(draws):
        starts = rng.integers(0, starts_max, size=n_blocks)
        sample = np.concatenate([values[s : s + block] for s in starts])[: len(values)]
        means[i] = sample.mean()
    return {
        "mean": float(values.mean()),
        "lo": float(np.percentile(means, 2.5)),
        "hi": float(np.percentile(means, 97.5)),
    }
