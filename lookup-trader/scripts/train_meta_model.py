#!/usr/bin/env python3
"""Train and compare meta-model candidates on the sparse event dataset.

    ./scripts/train_meta_model.py --dry-run   # fold sizes and leakage check only
    ./scripts/train_meta_model.py             # full run, writes the report

Offline by design: this writes a JSON report and nothing else. No artifact is
installed and no endpoint is wired, so the Evidence panel keeps reporting no
model until something here is worth promoting.

Read the report's `lift_vs_take_all`, not its net R. Taking every event earns
+0.0335R over 2025-2026H1 and -0.0515R over 2009-2024, so a model evaluated on
the audit block looks profitable whether or not it selects anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.ml.meta.folds import assert_no_overlap, audit_split, year_folds  # noqa: E402
from app.ml.meta.training import run  # noqa: E402
from app.services.meta_events import event_path  # noqa: E402

FIRST_TEST_YEAR = 2014
LAST_TEST_YEAR = 2024
AUDIT_FROM_YEAR = 2025


def report_path(symbol: str, timeframe: str) -> Path:
    return settings.data_dir / "reports" / f"meta-baseline-{symbol}-{timeframe}-v1.json"


def _load(symbol: str, timeframe: str) -> pd.DataFrame:
    path = event_path()
    if not path.exists():
        raise SystemExit(f"No meta-event export at {path} — run the exporter first.")
    frame = pd.read_parquet(path)
    frame = frame[(frame["symbol"] == symbol) & (frame["timeframe"] == timeframe)]
    return frame.sort_values("signal_ts").reset_index(drop=True)


def _describe(frame: pd.DataFrame) -> None:
    development, audit = audit_split(frame, audit_from_year=AUDIT_FROM_YEAR)
    dev = frame.iloc[development]
    aud = frame.iloc[audit]
    folds = year_folds(dev, first_test_year=FIRST_TEST_YEAR, last_test_year=LAST_TEST_YEAR)

    print(f"events {len(frame):,}   development {len(dev):,}   audit {len(aud):,}")
    print(
        f"take-all  development {dev.net_r_3.mean():+.4f}R   audit {aud.net_r_3.mean():+.4f}R"
    )
    print("\nfolds")
    print(f"  {'test':>6} {'train':>8} {'test n':>8} {'purged':>8}   train range")
    for fold in folds:
        train = dev.iloc[fold.train_idx]
        span = (
            f"{pd.to_datetime(train.signal_ts).min():%Y-%m} → "
            f"{pd.to_datetime(train.signal_ts).max():%Y-%m}"
            if len(train)
            else "—"
        )
        print(
            f"  {fold.test_year:>6} {len(fold.train_idx):>8,} {len(fold.test_idx):>8,} "
            f"{fold.purged:>8}   {span}"
        )
    assert_no_overlap(dev, folds)
    print("\nno training trade was still open when its test year began.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--dry-run", action="store_true", help="fold audit only")
    parser.add_argument("--no-tune", action="store_true", help="skip the Optuna search")
    parser.add_argument("--trials", type=int, default=40)
    args = parser.parse_args()

    symbol, timeframe = args.symbol.upper(), args.timeframe.upper()
    frame = _load(symbol, timeframe)
    _describe(frame)
    if args.dry_run:
        return 0

    _, audit_idx = audit_split(frame, audit_from_year=AUDIT_FROM_YEAR)
    print("\ntraining…")
    result = run(
        frame,
        first_test_year=FIRST_TEST_YEAR,
        last_test_year=LAST_TEST_YEAR,
        audit_idx=audit_idx,
        tune=not args.no_tune,
        trials=args.trials,
    )

    header = (
        f"\n{'candidate':18} {'log loss':>9} {'brier':>8} {'auc':>7} "
        f"{'take%':>7} {'net R':>9} {'lift':>9}"
    )
    print(header)
    for report in result["candidates"]:
        at = report["at_threshold"]
        auc = f"{report['auc']:.4f}" if report["auc"] is not None else "—"
        lift = at["lift_vs_take_all"]
        print(
            f"{report['name']:18} {report['log_loss']:>9.5f} {report['brier']:>8.5f} "
            f"{auc:>7} {at['take_rate']:>7.1%} "
            f"{at['net_r_3_per_event'] or 0:>+9.4f} {lift if lift is None else f'{lift:+.4f}':>9}"
        )

    audit = result["audit"]
    print(f"\nselected {result['selected']} at threshold {result['selected_threshold']:.2f}")
    print(f"audit take-all       {audit['take_all']['net_r_3_per_event']:+.4f}R")
    print(f"audit at threshold   {audit['at_threshold']['net_r_3_per_event'] or 0:+.4f}R "
          f"(take {audit['at_threshold']['take_rate']:.1%})")
    lift = audit["at_threshold"]["lift_vs_take_all"]
    shown = lift if lift is None else f"{lift:+.4f}"
    print(f"audit LIFT           {shown}R   <- the number that matters")
    ci = audit["bootstrap_net_r_3"]
    if ci["lo"] is not None:
        print(f"audit net R 95% CI   [{ci['lo']:+.4f}, {ci['hi']:+.4f}]")

    out = report_path(symbol, timeframe)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
