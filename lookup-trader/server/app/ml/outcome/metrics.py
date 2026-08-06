"""Evaluation metrics for calibrated multiclass outcome probabilities."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_fscore_support

from app.ml.outcome.model import CLASS_ORDER
from app.services.pips import pip_size, spread_pips


def multiclass_brier(y: pd.Series, probabilities: np.ndarray) -> float:
    targets = np.zeros_like(probabilities)
    for row, label in enumerate(y):
        targets[row, CLASS_ORDER.index(str(label))] = 1.0
    return float(np.mean(np.sum((probabilities - targets) ** 2, axis=1)))


def multiclass_log_loss(y: pd.Series, probabilities: np.ndarray) -> float:
    """Log loss using the artifact's explicit, non-lexicographic class order."""
    targets = np.array([CLASS_ORDER.index(str(label)) for label in y], dtype=int)
    selected = probabilities[np.arange(len(targets)), targets]
    return float(-np.log(np.clip(selected, 1e-15, 1.0)).mean())


def reliability_data(
    y: pd.Series, probabilities: np.ndarray, bins: int = 10
) -> dict[str, Any]:
    target = np.array([CLASS_ORDER.index(str(label)) for label in y])
    predicted = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)
    correct = predicted == target
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    ece = 0.0
    for index in range(bins):
        lower, upper = float(edges[index]), float(edges[index + 1])
        selected = (confidence >= lower) & (
            confidence <= upper if index == bins - 1 else confidence < upper
        )
        count = int(selected.sum())
        mean_confidence = float(confidence[selected].mean()) if count else None
        accuracy = float(correct[selected].mean()) if count else None
        if count:
            ece += count / len(y) * abs(float(mean_confidence) - float(accuracy))
        rows.append(
            {
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_confidence": mean_confidence,
                "accuracy": accuracy,
            }
        )
    return {"ece": float(ece), "bins": rows}


def _expectancy(frame: pd.DataFrame, y: pd.Series, probabilities: np.ndarray) -> dict[str, float]:
    gross_predicted = probabilities[:, CLASS_ORDER.index("win")] * 1.5
    gross_predicted -= probabilities[:, CLASS_ORDER.index("loss")]
    observed = y.map({"win": 1.5, "loss": -1.0, "timeout": 0.0}).to_numpy(dtype=float)
    atr = pd.to_numeric(frame["atr_at_signal"], errors="coerce").to_numpy(dtype=float)
    median_atr = float(np.nanmedian(atr)) if np.isfinite(atr).any() else 0.0
    atr = np.where(np.isfinite(atr) & (atr > 0), atr, median_atr)
    symbol = str(frame["symbol"].iloc[0]) if "symbol" in frame else "XAUUSD"
    spread_price = spread_pips(symbol) * pip_size(symbol)
    costs = np.divide(spread_price, atr, out=np.zeros_like(atr), where=atr > 0)
    return {
        "predicted_gross_r": float(np.mean(gross_predicted)),
        "predicted_net_r": float(np.mean(gross_predicted - costs)),
        "observed_gross_r": float(np.mean(observed)),
        "observed_net_r": float(np.mean(observed - costs)),
        "spread_pips_assumed": float(spread_pips(symbol)),
    }


def score_probabilities(
    frame: pd.DataFrame, y: pd.Series, probabilities: np.ndarray, *, detailed: bool = True
) -> dict[str, Any]:
    labels = list(CLASS_ORDER)
    predictions = np.asarray(labels)[probabilities.argmax(axis=1)]
    precision, recall, _, support = precision_recall_fscore_support(
        y, predictions, labels=labels, zero_division=0
    )
    result: dict[str, Any] = {
        "rows": len(y),
        "multiclass_log_loss": multiclass_log_loss(y, probabilities),
        "multiclass_brier": multiclass_brier(y, probabilities),
        "per_class": {
            label: {
                "precision": float(precision[index]),
                "recall": float(recall[index]),
                "support": int(support[index]),
            }
            for index, label in enumerate(labels)
        },
        "reliability": reliability_data(y, probabilities),
        "timeout_coverage": {
            "actual_rate": float((y == "timeout").mean()),
            "mean_predicted_probability": float(
                probabilities[:, CLASS_ORDER.index("timeout")].mean()
            ),
            "predicted_as_timeout_rate": float((predictions == "timeout").mean()),
        },
        "net_expectancy": _expectancy(frame, y, probabilities),
    }
    if not detailed:
        return result

    result["per_side"] = {}
    for side in sorted(frame["side"].unique()):
        selected = frame["side"].to_numpy() == side
        result["per_side"][str(int(side))] = score_probabilities(
            frame.loc[selected], y.loc[selected], probabilities[selected], detailed=False
        )
    regimes: dict[str, Any] = {}
    for column in ("session", "trend_state", "atr_bucket"):
        regimes[column] = {}
        values = frame[column].fillna("__missing__").astype(str)
        for value in sorted(values.unique()):
            selected = values.to_numpy() == value
            if int(selected.sum()) < 20:
                continue
            scored = score_probabilities(
                frame.loc[selected], y.loc[selected], probabilities[selected], detailed=False
            )
            regimes[column][value] = {
                "rows": scored["rows"],
                "multiclass_log_loss": scored["multiclass_log_loss"],
                "multiclass_brier": scored["multiclass_brier"],
                "observed_net_r": scored["net_expectancy"]["observed_net_r"],
            }
    result["key_regimes"] = regimes
    return result
