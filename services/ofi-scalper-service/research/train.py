"""Train one candidate per horizon: purged walk-forward CatBoost, calibrated in code.

For a horizon h, on a fixed ``Split``:

1. **Folds.** Fold k: fit the OFI PCA and a CatBoost multiclass model on the
   walk-forward periods before k (purged and embargoed against k), predict
   period k. Those are the out-of-fold (OOF) predictions.
2. **Calibration.** Isotonic regression per class (pool adjacent violators)
   fit on the OOF predictions, exported as the knots ``model.Calibrator`` reads.
   Brier score and a reliability curve per class go to ``cv_report.json``.
3. **Final model.** PCA and CatBoost fit on every walk-forward period. SHAP
   (mean |value| per feature and class) on a sample of its training rows.
4. **Scores.** OOF tables for each validation day, by the fold model that did
   not see it, calibrated. Selection backtests on these.

The held-out period is never read here. The candidate has no gates: only
``gates.py`` writes those, after the one evaluation on the held-out period.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ofi_scalper_service.model import CLASSES, Calibrator

from .cv import Split, purge
from .dataset import FEATURES, add_labels, apply_pca, fit_pca, label_column, load_days
from .scores import calibrate, score_frame, write_table

__all__ = [
    "CATBOOST_PARAMS",
    "Candidate",
    "brier_and_reliability",
    "isotonic_knots",
    "train_candidate",
]

CATBOOST_PARAMS: dict[str, Any] = {
    "loss_function": "MultiClass",
    "iterations": 400,
    "depth": 6,
    "learning_rate": 0.05,
    "l2_leaf_reg": 3.0,
    "random_seed": 7,
    "allow_writing_files": False,
    "verbose": False,
}
MAX_KNOTS = 200
SHAP_ROWS = 20_000
RELIABILITY_BINS = 10


@dataclass
class Candidate:
    """A trained, not yet gated, model for one horizon."""

    path: Path
    horizon_s: float
    barrier_bp: float
    features: list[str]
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Candidate:
        meta = json.loads((path / "candidate.json").read_text())
        return cls(path, meta["horizon_s"], meta["barrier_bp"], meta["features"], meta)

    @property
    def version(self) -> str:
        return self.path.name

    def calibrator(self) -> Calibrator:
        return Calibrator(json.loads((self.path / "calibrator.json").read_text()))

    def engine(self) -> dict[str, Any]:
        return json.loads((self.path / "engine.json").read_text())

    def oof_dir(self) -> Path:
        return self.path / "scores" / "oof"

    def final_dir(self) -> Path:
        return self.path / "scores" / "final"


# --- calibration ---------------------------------------------------------------------


def isotonic_knots(x: np.ndarray, y: np.ndarray, max_knots: int = MAX_KNOTS) -> dict[str, list]:
    """Non-decreasing fit of y on x (pool adjacent violators), as interpolation knots."""
    order = np.argsort(x, kind="stable")
    xs, ys = np.asarray(x, float)[order], np.asarray(y, float)[order]
    # Blocks: [sum_y, weight, sum_x]
    sums: list[float] = []
    weights: list[float] = []
    xsum: list[float] = []
    for xi, yi in zip(xs, ys, strict=True):
        sums.append(yi)
        weights.append(1.0)
        xsum.append(xi)
        while len(sums) > 1 and sums[-2] / weights[-2] > sums[-1] / weights[-1]:
            s, w, xx = sums.pop(), weights.pop(), xsum.pop()
            sums[-1] += s
            weights[-1] += w
            xsum[-1] += xx
    knots_x = np.array(xsum) / np.array(weights)
    knots_y = np.array(sums) / np.array(weights)
    if len(knots_x) > max_knots:
        pick = np.unique(np.linspace(0, len(knots_x) - 1, max_knots).round().astype(int))
        knots_x, knots_y = knots_x[pick], knots_y[pick]
    # Strictly increasing x (ties merged), at least two knots, within [0, 1].
    unique_x, index = np.unique(knots_x, return_index=True)
    unique_y = knots_y[index]
    if len(unique_x) < 2:
        level = float(unique_y[0]) if len(unique_y) else 0.0
        return {"x": [0.0, 1.0], "y": [level, level]}
    return {"x": [float(v) for v in unique_x], "y": [float(v) for v in np.clip(unique_y, 0, 1)]}


def brier_and_reliability(probs: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    """Per class: Brier score and a reliability curve (mean predicted vs observed)."""
    out: dict[str, Any] = {}
    edges = np.linspace(0, 1, RELIABILITY_BINS + 1)
    for j, name in enumerate(CLASSES):
        p, y = probs[:, j], (labels == j).astype(float)
        bins = []
        which = np.clip(np.digitize(p, edges) - 1, 0, RELIABILITY_BINS - 1)
        for b in range(RELIABILITY_BINS):
            mask = which == b
            if mask.any():
                bins.append(
                    {
                        "bin": [float(edges[b]), float(edges[b + 1])],
                        "n": int(mask.sum()),
                        "predicted": float(p[mask].mean()),
                        "observed": float(y[mask].mean()),
                    }
                )
        out[name] = {"brier": float(np.mean((p - y) ** 2)) if len(p) else None, "reliability": bins}
    return out


# --- training ------------------------------------------------------------------------


def _fit(train: pd.DataFrame, features: list[str], label: str, params: dict[str, Any]) -> Any:
    from catboost import CatBoostClassifier

    names = np.array(CLASSES)[train[label].to_numpy(dtype=int)]
    model = CatBoostClassifier(**params)
    model.fit(train[features].to_numpy(dtype=float), names)
    return model


def _predict(model: Any) -> Any:
    order = [str(c) for c in model.classes_]
    columns = [order.index(name) for name in CLASSES]

    def predict(x: np.ndarray) -> np.ndarray:
        return model.predict_proba(x)[:, columns]

    return predict


def _usable(df: pd.DataFrame, features: list[str], label: str) -> pd.DataFrame:
    return df[df[label].notna() & df[features].notna().all(axis=1)]


def _hash_files(paths: list[Path]) -> dict[str, str]:
    out = {}
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda h=handle: h.read(1 << 20), b""):
                digest.update(chunk)
        out[path.name] = digest.hexdigest()
    return out


def train_candidate(
    split: Split,
    *,
    features_dir: Path,
    out_dir: Path,
    horizon_s: float,
    barrier_bp: float,
    params: dict[str, Any] | None = None,
    max_train_rows: int | None = 2_000_000,
    features: list[str] | None = None,
) -> Candidate:
    """Fit folds, calibrate on OOF, fit the final model; write the candidate directory."""
    params = {**CATBOOST_PARAMS, **(params or {})}
    features = list(features or FEATURES)
    label = label_column(horizon_s)
    wf_days = split.days_of(split.walk_forward)
    df = add_labels(load_days(features_dir, wf_days), (horizon_s,), barrier_bp=barrier_bp)
    period_of_day = {day: p for p, days in split.days.items() for day in days}
    df["period"] = df["day"].map(period_of_day)
    rng = np.random.default_rng(params["random_seed"])

    def capped(frame: pd.DataFrame) -> pd.DataFrame:
        if max_train_rows is None or len(frame) <= max_train_rows:
            return frame
        keep = np.sort(rng.choice(len(frame), max_train_rows, replace=False))
        return frame.iloc[keep]

    path = out_dir
    path.mkdir(parents=True, exist_ok=True)
    oof_parts: list[pd.DataFrame] = []
    folds_report = []
    raw_oof: list[np.ndarray] = []
    oof_labels: list[np.ndarray] = []
    fold_models: list[tuple[str, Any, tuple[float, ...]]] = []
    for train_periods, validation in split.folds:
        validation_rows = df[df["period"] == validation]
        if validation_rows.empty:
            continue
        start = int(validation_rows["t_ns"].min())
        train = purge(df[df["period"].isin(train_periods)], start, horizon_s)
        weights = fit_pca(train)
        train = capped(_usable(apply_pca(train, weights), features, label))
        if train[label].nunique() < 3:
            folds_report.append({"validation": validation, "skipped": "fewer than 3 classes"})
            continue
        model = _fit(train, features, label, params)
        validation_rows = apply_pca(validation_rows, weights)
        usable = _usable(validation_rows, features, label)
        raw = _predict(model)(usable[features].to_numpy(dtype=float))
        raw_oof.append(raw)
        oof_labels.append(usable[label].to_numpy(dtype=int))
        fold_models.append((validation, model, weights))
        folds_report.append(
            {
                "train": list(train_periods),
                "validation": validation,
                "train_rows": len(train),
                "validation_rows": len(usable),
                "class_balance": {
                    name: float((usable[label] == j).mean()) for j, name in enumerate(CLASSES)
                },
            }
        )
        oof_parts.append(validation_rows)
    if not raw_oof:
        raise ValueError("no fold produced predictions: too little labelled data")

    raw_all, labels_all = np.vstack(raw_oof), np.concatenate(oof_labels)
    calibrator_json = {
        name: isotonic_knots(raw_all[:, j], (labels_all == j).astype(float))
        for j, name in enumerate(CLASSES)
    }
    calibrator = Calibrator(calibrator_json)
    calibrated = calibrate(raw_all, calibrator)

    # OOF score tables, per validation day, from the model that did not see it.
    for (_validation, model, _weights), rows in zip(fold_models, oof_parts, strict=True):
        for day, day_rows in rows.groupby("day"):
            table = score_frame(day_rows, features, _predict(model), calibrator)
            write_table(table, path / "scores" / "oof" / f"{day}.parquet")

    # Final model on every walk-forward period.
    weights = fit_pca(df)
    train_all = capped(_usable(apply_pca(df, weights), features, label))
    final = _fit(train_all, features, label, params)
    final.save_model(str(path / "model.cbm"))

    shap = _shap_summary(final, train_all, features, rng)
    report = {
        "horizon_s": horizon_s,
        "barrier_bp": barrier_bp,
        "folds": folds_report,
        "oof_rows": int(len(labels_all)),
        "raw": brier_and_reliability(raw_all, labels_all),
        "calibrated": brier_and_reliability(calibrated, labels_all),
        "params": params,
    }
    (path / "calibrator.json").write_text(json.dumps(calibrator_json, indent=2))
    (path / "engine.json").write_text(json.dumps({"pca_weights": list(weights)}, indent=2))
    (path / "cv_report.json").write_text(json.dumps(report, indent=2))
    (path / "shap_summary.json").write_text(json.dumps(shap, indent=2))
    meta = {
        "horizon_s": horizon_s,
        "barrier_bp": barrier_bp,
        "features": features,
        "split": split.run,
        "walk_forward": list(split.walk_forward),
        "data": _hash_files(
            [p for d in wf_days for p in (features_dir / f"{d}.parquet",) if p.is_file()]
        ),
        "final_train_rows": len(train_all),
    }
    (path / "candidate.json").write_text(json.dumps(meta, indent=2))
    return Candidate(path, horizon_s, barrier_bp, features, meta)


def _shap_summary(
    model: Any, train: pd.DataFrame, features: list[str], rng: np.random.Generator
) -> dict[str, Any]:
    from catboost import Pool

    sample = (
        train
        if len(train) <= SHAP_ROWS
        else train.iloc[rng.choice(len(train), SHAP_ROWS, replace=False)]
    )
    values = model.get_feature_importance(
        Pool(sample[features].to_numpy(dtype=float)), type="ShapValues"
    )  # (rows, classes, features + 1)
    order = [str(c) for c in model.classes_]
    out: dict[str, Any] = {"rows": len(sample), "mean_abs": {}}
    for name in CLASSES:
        j = order.index(name)
        mean_abs = np.abs(values[:, j, :-1]).mean(axis=0)
        ranked = sorted(zip(features, mean_abs, strict=True), key=lambda kv: -kv[1])
        out["mean_abs"][name] = {feature: float(v) for feature, v in ranked}
    return out


def score_final(candidate: Candidate, *, features_dir: Path, days: list[str]) -> list[Path]:
    """Score held-out days with the final model (gates only)."""
    from catboost import CatBoostClassifier

    model = CatBoostClassifier()
    model.load_model(str(candidate.path / "model.cbm"))
    weights = candidate.engine()["pca_weights"]
    calibrator = candidate.calibrator()
    written = []
    for day in days:
        rows = apply_pca(load_days(features_dir, [day]), weights)
        table = score_frame(rows, candidate.features, _predict(model), calibrator)
        written.append(write_table(table, candidate.final_dir() / f"{day}.parquet"))
    return written
