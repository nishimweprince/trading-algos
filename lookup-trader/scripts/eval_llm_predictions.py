#!/usr/bin/env python3
"""Score LLM predictions on the same basis as the CatBoost meta-model candidates.

    ./scripts/eval_llm_predictions.py --predictions preds.jsonl --split test

The point is a like-for-like comparison. This imports `app.ml.meta.metrics`, the
same module `train_meta_model.py` uses, so an LLM's number and CatBoost's number
mean the same thing and can be put side by side.

Predictions are JSONL, one object per line, joined on `event_id`:

    {"event_id": "8fc59b36-…", "take": true}
    {"event_id": "8fc59b36-…", "take": true, "probability": 0.61}

`probability` is optional but worth producing. Without it only the decision
metrics are available — take rate, net R, lift. With it you also get log loss,
Brier, AUC, calibration and the threshold sweep, which is what decides whether a
model is better or merely more aggressive. Extract it from `logprobs` on the
final `true`/`false` token; the export puts that token last for this reason.

Everything is reported as **lift over taking every event in the same block**.
Take-all earns +0.0335R across 2025-2026H1 and -0.0515R across 2009-2024, so a
model that selects nothing looks profitable on the test split if you read its
absolute net R.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.ml.meta import metrics as M  # noqa: E402

SPLITS = ("train", "validation", "test")


def split_path(split: str) -> Path:
    """Outcome truth. Lives beside the upload file, not inside it.

    The upload JSONL carries `messages` and nothing else, because a fine-tuning
    platform rejects the entire file over one unknown top-level key.
    """
    return settings.data_dir / "exports" / "llm" / f"meta-events-{split}.metadata.jsonl"


def report_path(split: str) -> Path:
    return settings.data_dir / "reports" / f"llm-eval-XAUUSD-H1-{split}.json"


def _truth(split: str) -> pd.DataFrame:
    """Outcome truth from the exported split's metadata."""
    path = split_path(split)
    if not path.exists():
        raise SystemExit(f"No export at {path} — run scripts/export_llm_dataset.py first.")
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]
    frame = pd.DataFrame(rows)
    frame["signal_ts"] = pd.to_datetime(frame["signal_ts"], utc=True)
    return frame


def _predictions(path: Path) -> tuple[pd.DataFrame, dict[str, int]]:
    """Parse predictions, counting what could not be read.

    A fine-tuned model emits text, so malformed JSON is a real failure mode
    rather than a hypothetical. Unparseable rows are counted and dropped rather
    than silently coerced to a decision the model did not make.
    """
    parsed: list[dict[str, Any]] = []
    audit = {"lines": 0, "unparseable": 0, "missing_event_id": 0, "missing_take": 0}
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        audit["lines"] += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            audit["unparseable"] += 1
            continue
        # Tolerate a nested completion string, which is what raw API output
        # looks like before post-processing.
        if "take" not in row and isinstance(row.get("content"), str):
            try:
                row = {**row, **json.loads(row["content"])}
            except json.JSONDecodeError:
                audit["unparseable"] += 1
                continue
        if not row.get("event_id"):
            audit["missing_event_id"] += 1
            continue
        if "take" not in row and "probability" not in row:
            audit["missing_take"] += 1
            continue
        parsed.append(row)
    return pd.DataFrame(parsed), audit


def _score(frame: pd.DataFrame, probability: np.ndarray | None, take: np.ndarray) -> dict:
    """Decision metrics always; probability metrics only when probabilities exist."""
    base = frame["net_r_3"].mean()
    selected = frame.loc[take]
    decision: dict[str, Any] = {
        "events": int(len(frame)),
        "taken": int(take.sum()),
        "take_rate": float(take.mean()),
        "take_all_net_r_3": float(base),
    }
    if len(selected):
        decision.update(
            {
                "net_r_3_per_event": float(selected["net_r_3"].mean()),
                "net_r_5_per_event": float(selected["net_r_5"].mean()),
                "net_r_8_per_event": float(selected["net_r_8"].mean()),
                "total_net_r_3": float(selected["net_r_3"].sum()),
                "win_rate": float(selected["y_meta"].mean()),
                # The only figure comparable across blocks.
                "lift_vs_take_all": float(selected["net_r_3"].mean() - base),
                "bootstrap_lift": M.block_bootstrap_ci(
                    selected["net_r_3"].to_numpy() - base
                ),
            }
        )
    else:
        decision["lift_vs_take_all"] = None

    out: dict[str, Any] = {"decision": decision}
    if probability is None:
        out["probability_metrics"] = None
        return out

    y = frame["y_meta"].to_numpy()
    out["probability_metrics"] = {
        **M.probability_scores(y, probability),
        "reliability": M.reliability(y, probability),
        "sweep": M.sweep(frame, probability),
    }
    return out


def _permutation_p(frame: pd.DataFrame, take: np.ndarray, draws: int = 20000) -> float | None:
    """Does this selection beat a random subset of the same size?

    On a block where take-all is already profitable, any subset tends to look
    profitable. This asks the sharper question.
    """
    net = frame["net_r_3"].to_numpy()
    k = int(take.sum())
    if k == 0 or k == len(net):
        return None
    observed = net[take].mean()
    rng = np.random.default_rng(0)
    sampled = np.array(
        [net[rng.choice(len(net), size=k, replace=False)].mean() for _ in range(draws)]
    )
    return float((sampled >= observed).mean())


def _stability(frame: pd.DataFrame, take: np.ndarray) -> dict[str, dict]:
    work = frame.assign(_take=take, _year=frame["signal_ts"].dt.year)
    out: dict[str, dict] = {}
    slices = (("by_year", "_year"), ("by_side", "side"), ("by_setup", "primary_setup_id"))
    for key, column in slices:
        cells = {}
        for value, group in work.groupby(column):
            selected = group.loc[group["_take"]]
            cells[str(value)] = {
                "events": int(len(group)),
                "taken": int(len(selected)),
                "lift_vs_take_all": (
                    float(selected["net_r_3"].mean() - group["net_r_3"].mean())
                    if len(selected)
                    else None
                ),
            }
        out[key] = cells
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--split", choices=SPLITS, default="test")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Re-derive take/skip from `probability` at this cut, not the model's decision.",
    )
    parser.add_argument("--write", action="store_true", help="write the JSON report")
    args = parser.parse_args()

    truth = _truth(args.split)
    predictions, audit = _predictions(args.predictions)
    if predictions.empty:
        raise SystemExit(f"No usable predictions in {args.predictions}: {audit}")

    merged = truth.merge(predictions, on="event_id", how="inner", suffixes=("", "_pred"))
    coverage = len(merged) / len(truth)
    print(f"split {args.split}: {len(truth):,} events, {len(merged):,} scored ({coverage:.1%})")
    if audit["unparseable"] or audit["missing_event_id"] or audit["missing_take"]:
        print(f"  prediction parse audit: {audit}")
    if coverage < 1.0:
        # Silently scoring a subset is how a model that abstained on hard cases
        # comes out looking selective.
        print(f"  WARNING: {len(truth) - len(merged):,} events have no prediction and are excluded")

    # Probability metrics run on whatever subset carries a probability. A few
    # rows where logprobs did not come back should cost those rows, not the
    # whole log-loss/AUC/calibration picture.
    has_probability = (
        merged["probability"].notna()
        if "probability" in merged
        else pd.Series(False, index=merged.index)
    )
    probability_coverage = float(has_probability.mean())

    if args.threshold is not None:
        if not has_probability.all():
            raise SystemExit(
                f"--threshold needs a probability on every row; "
                f"{(~has_probability).sum():,} are missing"
            )
        take = (merged["probability"].to_numpy(dtype=float) >= args.threshold)
    else:
        take = merged["take"].astype(bool).to_numpy()

    scored_probability = (
        merged.loc[has_probability, "probability"].to_numpy(dtype=float)
        if probability_coverage > 0
        else None
    )
    result = _score(merged, None, take)
    if scored_probability is not None:
        subset = merged.loc[has_probability]
        result["probability_metrics"] = _score(
            subset, scored_probability, take[has_probability.to_numpy()]
        )["probability_metrics"]
        result["probability_coverage"] = probability_coverage
        if probability_coverage < 1.0:
            print(
                f"  probability present on {probability_coverage:.1%} of rows; "
                f"log loss / AUC computed on that subset"
            )
    result["split"] = args.split
    result["coverage"] = coverage
    result["prediction_audit"] = audit
    result["permutation_p_value"] = _permutation_p(merged, take)
    result["stability"] = _stability(merged, take)

    decision = result["decision"]
    print(f"\n{'':22} {'value':>12}")
    print(f"{'taken':22} {decision['taken']:>12,} ({decision['take_rate']:.1%})")
    print(f"{'take-all net R':22} {decision['take_all_net_r_3']:>+12.4f}")
    if decision.get("net_r_3_per_event") is not None:
        print(f"{'selected net R':22} {decision['net_r_3_per_event']:>+12.4f}")
        print(f"{'LIFT vs take-all':22} {decision['lift_vs_take_all']:>+12.4f}   <- the number")
        ci = decision["bootstrap_lift"]
        if ci["lo"] is not None:
            span = f"[{ci['lo']:+.4f}, {ci['hi']:+.4f}]"
            print(f"{'lift 95% CI':22} {span:>12}")
        print(f"{'win rate':22} {decision['win_rate']:>12.2%}")
    if result["permutation_p_value"] is not None:
        print(f"{'permutation p':22} {result['permutation_p_value']:>12.4f}")

    metrics = result["probability_metrics"]
    if metrics:
        print(f"\n{'log loss':22} {metrics['log_loss']:>12.5f}")
        print(f"{'brier':22} {metrics['brier']:>12.5f}")
        auc = metrics["auc"]
        print(f"{'auc':22} {auc if auc is None else f'{auc:>12.4f}'}")
        print(f"{'ece':22} {metrics['ece']:>12.4f}")
        print("\nCatBoost on its own OOF block scored log loss 0.67860, AUC 0.521.")
    else:
        print("\nNo `probability` field: decision metrics only.")
        print("Add one from logprobs to get log loss, AUC and the threshold sweep.")

    years = result["stability"]["by_year"]
    positive = sum(1 for v in years.values() if (v["lift_vs_take_all"] or 0) > 0)
    print(f"\nper-year lift positive in {positive}/{len(years)} years")

    if args.write:
        out = report_path(args.split)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
