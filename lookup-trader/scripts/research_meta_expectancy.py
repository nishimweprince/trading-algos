#!/usr/bin/env python3
"""Accuracy-trained vs expectancy-trained meta-model variants, plus trend splits.

    ./scripts/research_meta_expectancy.py
    ./scripts/research_meta_expectancy.py --gate trend_aligned
    ./scripts/research_meta_expectancy.py --timeframe M15 \\
        --events data/exports/meta_events_v1_M15.parquet

Research only: writes a JSON report under data/reports and nothing else.

Why it exists. The production label `y_meta` is `net_r_3 > 0`, an accuracy
target: a +0.05R timeout counts the same as a +1.45R target hit. This script asks
whether training on expectancy instead — sample weights, the target-hit label,
or a regression on net R — or blocking negative-expectancy setups, finds a
selection that survives development out-of-fold.

Every variant uses the same features, the same purged year folds (test
2014-2024), CatBoost defaults and one selection rule: the take rate is chosen on
development OOF net R only, and that frozen quantile cut is applied once to the
2025+ audit block. Read `pos_years` and `dev_net` before `aud_net` — taking every
event already earns a positive audit net in the 2025-2026 regime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.ml.meta.folds import audit_split, year_folds  # noqa: E402
from app.ml.meta.gates import GATES, apply_gate  # noqa: E402
from app.ml.meta.metrics import block_bootstrap_ci  # noqa: E402
from app.ml.meta.training import RANDOM_STATE, CatBoostCandidate  # noqa: E402
from app.services.meta_events import event_path  # noqa: E402

FIRST_TEST_YEAR = 2014
LAST_TEST_YEAR = 2024
AUDIT_FROM_YEAR = 2025
TAKE_RATES = (0.05, 0.075, 0.10, 0.15, 0.20, 0.30, 0.50)
MIN_TAKEN = 200
MIN_SETUP_N = 100
# CatBoostCandidate's defaults, so variants differ only in label/loss/weights.
PARAMS = {
    "iterations": 400,
    "learning_rate": 0.03,
    "depth": 4,
    "l2_leaf_reg": 6.0,
    "random_seed": RANDOM_STATE,
    "verbose": False,
    "allow_writing_files": False,
}

VARIANTS = (
    ("take_all", "take_all", False),
    ("accuracy_clf", "clf_acc", False),
    ("accuracy_clf_r_weighted", "clf_acc_weighted", False),
    ("target_hit_clf", "clf_target", False),
    ("expectancy_regressor", "reg_net", False),
    ("setup_blocklist", "take_all", True),
    ("setup_blocklist_accuracy_clf", "clf_acc", True),
    ("setup_blocklist_regressor", "reg_net", True),
)


def _design(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    return CatBoostCandidate()._design(frame)


def fit_predict(kind: str, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    from catboost import CatBoostClassifier, CatBoostRegressor

    xt, cats = _design(train)
    xs = _design(test)[0].loc[:, list(xt.columns)]
    if kind == "clf_acc":
        model = CatBoostClassifier(loss_function="Logloss", **PARAMS)
        model.fit(xt, train["y_meta"].to_numpy(), cat_features=cats)
        return model.predict_proba(xs)[:, 1]
    if kind == "clf_acc_weighted":
        # Cost-sensitive: misjudging a +1.45R target or a -1R stop costs more
        # than misjudging a +0.05R timeout.
        model = CatBoostClassifier(loss_function="Logloss", **PARAMS)
        weights = np.abs(train["net_r_3"].to_numpy()) + 1e-3
        model.fit(xt, train["y_meta"].to_numpy(), cat_features=cats, sample_weight=weights)
        return model.predict_proba(xs)[:, 1]
    if kind == "clf_target":
        model = CatBoostClassifier(loss_function="Logloss", **PARAMS)
        model.fit(xt, (train["outcome"] == "win").astype(int).to_numpy(), cat_features=cats)
        return model.predict_proba(xs)[:, 1]
    if kind == "reg_net":
        model = CatBoostRegressor(loss_function="RMSE", **PARAMS)
        model.fit(xt, train["net_r_3"].to_numpy(), cat_features=cats)
        return model.predict(xs)
    raise ValueError(kind)


def setup_blocklist(train: pd.DataFrame) -> set[str]:
    """Setups whose history so far is negative-expectancy with enough samples."""
    g = train.groupby("primary_setup_id")["net_r_3"].agg(["size", "mean"])
    return set(g[(g["size"] >= MIN_SETUP_N) & (g["mean"] < 0)].index)


def _oof(dev: pd.DataFrame, folds, kind: str, blocklist: bool) -> np.ndarray:
    scores = np.full(len(dev), np.nan)
    for fold in folds:
        if len(fold.train_idx) == 0 or len(fold.test_idx) == 0:
            continue
        train, test = dev.iloc[fold.train_idx], dev.iloc[fold.test_idx]
        s = np.zeros(len(test)) if kind == "take_all" else fit_predict(kind, train, test)
        if blocklist:
            s = np.where(test["primary_setup_id"].isin(setup_blocklist(train)), -np.inf, s)
        scores[fold.test_idx] = s
    return scores


def _choose_cut(block: pd.DataFrame, scores: np.ndarray) -> tuple[float | None, float]:
    """Best development take rate by OOF net R, as a cut in score units."""
    finite = scores[np.isfinite(scores)]
    best: tuple[float, float, float] | None = None
    for rate in TAKE_RATES:
        level = float(np.clip(1 - rate * len(scores) / max(len(finite), 1), 0.0, 1.0))
        cut = float(np.quantile(finite, level)) if len(finite) else float("inf")
        taken = block[scores >= cut]
        if len(taken) >= MIN_TAKEN and (best is None or taken["net_r_3"].mean() > best[2]):
            best = (rate, cut, float(taken["net_r_3"].mean()))
    if best is None:
        return None, float("inf")
    return best[0], best[1]


def evaluate(frame: pd.DataFrame, name: str, kind: str, blocklist: bool) -> dict:
    development, audit = audit_split(frame, audit_from_year=AUDIT_FROM_YEAR)
    dev = frame.iloc[development].reset_index(drop=True)
    aud = frame.iloc[audit].reset_index(drop=True)
    folds = year_folds(dev, first_test_year=FIRST_TEST_YEAR, last_test_year=LAST_TEST_YEAR)

    scores = _oof(dev, folds, kind, blocklist)
    scored = ~np.isnan(scores)
    d, ds = dev[scored].reset_index(drop=True), scores[scored]
    if kind == "take_all":
        rate, cut = None, -float("inf")
    else:
        rate, cut = _choose_cut(d, ds)
    # Blocked events score -inf; exclude them explicitly so a -inf take-all cut
    # cannot readmit them.
    dt = d[(ds >= cut) & np.isfinite(ds)]
    years = pd.to_datetime(dt["signal_ts"]).dt.year
    by_year = dt.groupby(years)["net_r_3"].mean()

    audit_scores = np.zeros(len(aud)) if kind == "take_all" else fit_predict(kind, dev, aud)
    if blocklist:
        blocked = aud["primary_setup_id"].isin(setup_blocklist(dev))
        audit_scores = np.where(blocked, -np.inf, audit_scores)
    at = aud[(audit_scores >= cut) & np.isfinite(audit_scores)]
    ci = block_bootstrap_ci(at["net_r_3"].to_numpy()) if len(at) else {"lo": None, "hi": None}
    return {
        "variant": name,
        "take_rate_chosen": rate,
        "dev_taken": int(len(dt)),
        "dev_take_share": float(len(dt) / len(d)) if len(d) else 0.0,
        "dev_net": float(dt["net_r_3"].mean()) if len(dt) else None,
        "dev_target_rate": float((dt["outcome"] == "win").mean()) if len(dt) else None,
        "dev_accuracy": float((dt["net_r_3"] > 0).mean()) if len(dt) else None,
        "pos_years": int((by_year > 0).sum()),
        "years": int(by_year.size),
        "aud_taken": int(len(at)),
        "aud_net": float(at["net_r_3"].mean()) if len(at) else None,
        "aud_lift": float(at["net_r_3"].mean() - aud["net_r_3"].mean()) if len(at) else None,
        "aud_ci": [ci["lo"], ci["hi"]],
        "aud_long_share": float((at["side"] == 1).mean()) if len(at) else None,
    }


def trend_splits(frame: pd.DataFrame) -> list[dict]:
    """Take-all by trend alignment: the evidence behind the `trend_aligned` gate."""
    years = pd.to_datetime(frame["signal_ts"]).dt.year
    dev, aud = frame[years < AUDIT_FROM_YEAR], frame[years >= AUDIT_FROM_YEAR]
    ltf = "trend_state"
    htf = "htf_trend_state"
    rules = {
        "all": lambda x: pd.Series(True, index=x.index),
        "htf_aligned": lambda x: x[htf].eq("aligned"),
        "htf_opposed": lambda x: x[htf].eq("opposed"),
        "ltf_htf_aligned": lambda x: x[ltf].eq("aligned") & x[htf].eq("aligned"),
        "ltf_htf_aligned_longs": lambda x: (
            x[ltf].eq("aligned") & x[htf].eq("aligned") & x["side"].eq(1)
        ),
        "ltf_htf_aligned_shorts": lambda x: (
            x[ltf].eq("aligned") & x[htf].eq("aligned") & x["side"].eq(-1)
        ),
    }
    rows = []
    for name, rule in rules.items():
        d, a = dev[rule(dev)], aud[rule(aud)]
        by_year = d.groupby(pd.to_datetime(d["signal_ts"]).dt.year)["net_r_3"].mean()
        sd = d["net_r_3"].std()
        rows.append(
            {
                "split": name,
                "dev_n": int(len(d)),
                "dev_net": float(d["net_r_3"].mean()),
                "dev_gross": float(d["gross_r"].mean()),
                "dev_t": float(d["net_r_3"].mean() / sd * np.sqrt(len(d))) if sd else None,
                "pos_years": int((by_year > 0).sum()),
                "years": int(by_year.size),
                "aud_n": int(len(a)),
                "aud_net": float(a["net_r_3"].mean()) if len(a) else None,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--gate", choices=sorted(GATES), default="none")
    args = parser.parse_args()

    symbol, timeframe = args.symbol.upper(), args.timeframe.upper()
    path = args.events or event_path()
    frame = pd.read_parquet(path)
    frame = frame[(frame["symbol"] == symbol) & (frame["timeframe"] == timeframe)]
    frame = frame.sort_values("signal_ts").reset_index(drop=True)

    splits = trend_splits(frame)
    gated = apply_gate(frame, args.gate)
    print(f"{symbol} {timeframe}  gate={args.gate}  kept {len(gated):,}/{len(frame):,} events")

    pd.set_option("display.width", 250)
    print("\ntrend splits (ungated take-all)")
    print(pd.DataFrame(splits).to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    rows = []
    for name, kind, blocklist in VARIANTS:
        rows.append(evaluate(gated, name, kind, blocklist))
        print(f"  done {name}", flush=True)
    table = pd.DataFrame(rows)
    table["aud_ci"] = table["aud_ci"].map(
        lambda ci: "-" if ci[0] is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"
    )
    print("\nvariants")
    print(table.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    suffix = "" if args.gate == "none" else f"-gate-{args.gate}"
    out = settings.data_dir / "reports" / f"meta-expectancy-{symbol}-{timeframe}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "events": str(path),
        "gate": args.gate,
        "kept": int(len(gated)),
        "of": int(len(frame)),
        "trend_splits": splits,
        "variants": rows,
        "blocklist_on_development": sorted(
            setup_blocklist(gated.iloc[audit_split(gated, audit_from_year=AUDIT_FROM_YEAR)[0]])
        ),
    }
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
