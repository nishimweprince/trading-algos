"""Outcome estimators, calibration, and empirical frequency baseline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

CLASS_ORDER = ("win", "loss", "timeout")
CONTEXT_COLUMNS = ("side", "session", "trend_state", "atr_bucket")


def reorder_probabilities(
    probabilities: np.ndarray, estimator_classes: tuple[str, ...] | np.ndarray
) -> np.ndarray:
    positions = {str(label): index for index, label in enumerate(estimator_classes)}
    missing = sorted(set(CLASS_ORDER) - set(positions))
    if missing:
        raise ValueError(f"Estimator is missing outcome classes: {missing}")
    return probabilities[:, [positions[label] for label in CLASS_ORDER]]


def calibrate_probabilities(probabilities: np.ndarray, temperature: float) -> np.ndarray:
    clipped = np.clip(probabilities, 1e-15, 1.0)
    logits = np.log(clipped) / temperature
    logits -= logits.max(axis=1, keepdims=True)
    calibrated = np.exp(logits)
    return calibrated / calibrated.sum(axis=1, keepdims=True)


def fit_temperature(probabilities: np.ndarray, y: pd.Series) -> float:
    """Fit one deterministic multiclass temperature on a calibration block."""
    targets = np.array([CLASS_ORDER.index(str(label)) for label in y], dtype=int)

    def loss(temperature: float) -> float:
        calibrated = calibrate_probabilities(probabilities, temperature)
        target_probabilities = calibrated[np.arange(len(targets)), targets]
        return float(-np.log(np.clip(target_probabilities, 1e-15, 1)).mean())

    temperatures = np.geomspace(0.15, 6.0, 400)
    losses = np.array([loss(float(value)) for value in temperatures])
    return float(temperatures[int(np.argmin(losses))])


@dataclass
class CalibratedOutcomeModel:
    """Serializable preprocessing + estimator + held-out temperature."""

    preprocessor: object
    estimator: object
    temperature: float
    class_order: tuple[str, ...] = CLASS_ORDER

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        transformed = self.preprocessor.transform(X)
        raw = self.estimator.predict_proba(transformed)
        ordered = reorder_probabilities(raw, self.estimator.classes_)
        calibrated = calibrate_probabilities(ordered, self.temperature)
        if not np.allclose(calibrated.sum(axis=1), 1.0, atol=1e-12):
            raise ValueError("Calibrated probabilities do not sum to one")
        return calibrated

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        probabilities = self.predict_proba(X)
        return np.asarray(self.class_order)[probabilities.argmax(axis=1)]


class ContextFrequencyBaseline:
    """Smoothed side/context frequencies with a global fallback."""

    def __init__(self, smoothing: float = 10.0) -> None:
        self.smoothing = smoothing

    def fit(self, X: pd.DataFrame, y: pd.Series) -> ContextFrequencyBaseline:
        data = X.loc[:, CONTEXT_COLUMNS].copy().fillna("__missing__")
        data["_target"] = y.to_numpy()
        global_counts = data["_target"].value_counts()
        self.global_probabilities_ = np.array(
            [float(global_counts.get(label, 0)) for label in CLASS_ORDER], dtype=float
        )
        self.global_probabilities_ /= self.global_probabilities_.sum()
        self.context_probabilities_: dict[tuple[str, ...], np.ndarray] = {}
        for key, group in data.groupby(list(CONTEXT_COLUMNS), dropna=False, sort=True):
            counts = group["_target"].value_counts()
            raw = np.array([float(counts.get(label, 0)) for label in CLASS_ORDER])
            smoothed = raw + self.smoothing * self.global_probabilities_
            self.context_probabilities_[tuple(str(value) for value in key)] = (
                smoothed / smoothed.sum()
            )
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        contexts = X.loc[:, CONTEXT_COLUMNS].fillna("__missing__")
        return np.vstack(
            [
                self.context_probabilities_.get(
                    tuple(str(value) for value in row), self.global_probabilities_
                )
                for row in contexts.itertuples(index=False, name=None)
            ]
        )
