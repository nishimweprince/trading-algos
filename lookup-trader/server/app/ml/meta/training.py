"""Fit and compare meta-model candidates over chronological folds.

The order is fixed by the roadmap: take-all, event frequency, regularised
logistic, then CatBoost with chronological Optuna tuning. Candidates are ranked
on out-of-fold log loss — a probability quality measure — and only then is a
take threshold chosen on net R. Tuning directly against net R would be fitting
the payoff of a few hundred trades.

The audit block is read exactly once, at the end, after the threshold is frozen.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from app.ml.meta import metrics as M
from app.ml.meta.baselines import EventFrequency, TakeAll, build_preprocessor, expand_features
from app.ml.meta.folds import YearFold, year_folds

RANDOM_STATE = 0
OPTUNA_TRIALS = 40


class SklearnCandidate:
    """A scikit-learn estimator behind the shared preprocessor."""

    def __init__(self, name: str, factory) -> None:
        self.name, self._factory = name, factory

    def fit(self, frame: pd.DataFrame, y: pd.Series) -> SklearnCandidate:
        self._pre = build_preprocessor()
        design = self._pre.fit_transform(expand_features(frame))
        self._est = self._factory()
        self._est.fit(design, np.asarray(y))
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        design = self._pre.transform(expand_features(frame))
        return self._est.predict_proba(design)[:, 1]


class CatBoostCandidate:
    """CatBoost over raw columns, using its own categorical handling.

    No one-hot and no scaling: ordered target statistics are the reason to reach
    for CatBoost at this sample size, and they need the raw categories. The
    shape vector is expanded because it is 48 separate numeric features.
    """

    name = "catboost"

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        *,
        feature_columns: tuple[str, ...] | None = None,
    ) -> None:
        from app.ml.meta.features import META_INPUT_FEATURES

        self.params = params or {}
        self.feature_columns = feature_columns or META_INPUT_FEATURES

    def _design(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        from app.ml.meta.features import META_CATEGORICAL_FEATURES

        design = expand_features(frame, self.feature_columns)
        categorical = [name for name in META_CATEGORICAL_FEATURES if name in design.columns]
        for name in categorical:
            design[name] = design[name].astype(str)
        return design, categorical

    def fit(self, frame: pd.DataFrame, y: pd.Series) -> CatBoostCandidate:
        from catboost import CatBoostClassifier

        design, cats = self._design(frame)
        self._columns = list(design.columns)
        self._est = CatBoostClassifier(
            loss_function="Logloss",
            random_seed=RANDOM_STATE,
            verbose=False,
            allow_writing_files=False,
            **{
                "iterations": 400,
                "learning_rate": 0.03,
                "depth": 4,
                "l2_leaf_reg": 6.0,
                **self.params,
            },
        )
        self._est.fit(design, np.asarray(y), cat_features=cats)
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        design, _ = self._design(frame)
        return self._est.predict_proba(design.loc[:, self._columns])[:, 1]


def _oof(frame: pd.DataFrame, folds: list[YearFold], build) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold probabilities and the positions they belong to."""
    parts, positions = [], []
    for fold in folds:
        if len(fold.train_idx) == 0 or len(fold.test_idx) == 0:
            continue
        train, test = frame.iloc[fold.train_idx], frame.iloc[fold.test_idx]
        model = build().fit(train, train["y_meta"])
        parts.append(model.predict_proba(test))
        positions.append(fold.test_idx)
    return np.concatenate(parts), np.concatenate(positions)


def evaluate(frame: pd.DataFrame, folds: list[YearFold], build, *, name: str) -> dict[str, Any]:
    """Out-of-fold scores for one candidate, plus per-fold lift over take-all."""
    p, positions = _oof(frame, folds, build)
    scored = frame.iloc[positions]
    y = scored["y_meta"].to_numpy()

    threshold = M.choose_threshold(scored, p)
    report: dict[str, Any] = {
        "name": name,
        "oof_events": int(len(scored)),
        **M.probability_scores(y, p),
        "chosen_threshold": threshold,
        "at_threshold": M.at_threshold(scored, p, threshold),
        "take_all": M.take_all(scored),
        "sweep": M.sweep(scored, p),
    }

    per_fold = {}
    for fold in folds:
        mask = np.isin(positions, fold.test_idx)
        if not mask.any():
            continue
        block = frame.iloc[positions[mask]]
        per_fold[str(fold.test_year)] = M.at_threshold(block, p[mask], threshold)
    report["per_fold"] = per_fold
    report["stability"] = M.stability(scored, p, threshold)
    report["_oof_p"] = p
    report["_oof_positions"] = positions
    return report


def tune_catboost(frame: pd.DataFrame, folds: list[YearFold], trials: int = OPTUNA_TRIALS):
    """Chronological Optuna search, scored on out-of-fold log loss."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial) -> float:
        params = {
            "iterations": trial.suggest_int("iterations", 200, 900, step=100),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.12, log=True),
            "depth": trial.suggest_int("depth", 3, 7),
            "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1.0, 20.0, log=True),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
        }
        p, positions = _oof(frame, folds, lambda: CatBoostCandidate(params))
        from sklearn.metrics import log_loss

        return float(log_loss(frame.iloc[positions]["y_meta"].to_numpy(), p, labels=[0, 1]))

    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(seed=RANDOM_STATE)
    )
    study.optimize(objective, n_trials=trials, show_progress_bar=False)
    return study.best_params, float(study.best_value)


def candidates() -> list[tuple[str, Any]]:
    """Roadmap Phase 7 steps 1-3; CatBoost is added separately after tuning."""
    return [
        ("take_all", TakeAll),
        ("event_frequency", EventFrequency),
        (
            "logistic",
            lambda: SklearnCandidate(
                "logistic",
                lambda: LogisticRegression(
                    max_iter=2000, C=0.1, solver="lbfgs", random_state=RANDOM_STATE
                ),
            ),
        ),
    ]


def run(
    frame: pd.DataFrame,
    *,
    first_test_year: int,
    last_test_year: int,
    audit_idx: np.ndarray,
    tune: bool = True,
    trials: int = OPTUNA_TRIALS,
) -> dict[str, Any]:
    """Fit every candidate over the development folds and audit the winner once."""
    development = frame.drop(index=frame.index[audit_idx]).reset_index(drop=True)
    audit = frame.iloc[audit_idx].reset_index(drop=True)
    folds = year_folds(development, first_test_year=first_test_year, last_test_year=last_test_year)

    reports = [evaluate(development, folds, build, name=name) for name, build in candidates()]

    catboost_params: dict[str, Any] = {}
    tuning: dict[str, Any] = {}
    if tune:
        catboost_params, best_loss = tune_catboost(development, folds, trials)
        tuning = {"trials": trials, "best_log_loss": best_loss, "best_params": catboost_params}
    reports.append(
        evaluate(
            development,
            folds,
            lambda: CatBoostCandidate(catboost_params),
            name="catboost",
        )
    )

    # Selection is on probability quality, never on net R.
    ranked = sorted((r for r in reports if r["name"] != "take_all"), key=lambda r: r["log_loss"])
    winner = ranked[0]

    # Only now does the audit block get read, with the threshold already frozen.
    build = dict(candidates()).get(winner["name"]) or (lambda: CatBoostCandidate(catboost_params))
    final = build().fit(development, development["y_meta"])
    audit_p = final.predict_proba(audit)
    audit_report = {
        **M.probability_scores(audit["y_meta"].to_numpy(), audit_p),
        "at_threshold": M.at_threshold(audit, audit_p, winner["chosen_threshold"]),
        "take_all": M.take_all(audit),
        "bootstrap_net_r_3": M.block_bootstrap_ci(
            audit.loc[audit_p >= winner["chosen_threshold"], "net_r_3"].to_numpy()
        ),
    }

    for report in reports:
        report.pop("_oof_p", None)
        report.pop("_oof_positions", None)

    return {
        "folds": [
            {
                "test_year": f.test_year,
                "train_events": int(len(f.train_idx)),
                "test_events": int(len(f.test_idx)),
                "purged": f.purged,
            }
            for f in folds
        ],
        "candidates": reports,
        "tuning": tuning,
        "selected": winner["name"],
        "selected_threshold": winner["chosen_threshold"],
        "audit": audit_report,
    }
