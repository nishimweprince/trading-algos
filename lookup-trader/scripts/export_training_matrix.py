#!/usr/bin/env python3
"""Offline export of bar_features to a training matrix."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.services.export_bar_features import export_bar_features  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the leakage-proof XAUUSD H1 outcome-v1 dataset"
    )
    parser.add_argument("--symbol", default="XAUUSD", choices=["XAUUSD"])
    parser.add_argument("--timeframe", default="H1", choices=["H1"])
    parser.add_argument("--from", dest="date_from", type=datetime.fromisoformat, default=None)
    parser.add_argument("--to", dest="date_to", type=datetime.fromisoformat, default=None)
    parser.add_argument("--horizon", type=int, default=24, choices=[24])
    parser.add_argument("--target-atr", type=float, default=1.5, choices=[1.5])
    parser.add_argument("--stop-atr", type=float, default=1.0, choices=[1.0])
    parser.add_argument("--n-splits", type=int, default=5)
    parser.add_argument("--embargo-bars", type=int, default=None)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.data_dir / "exports" / "bar_features_training.parquet",
    )
    args = parser.parse_args()

    con = duckdb.connect(":memory:")
    count = export_bar_features(
        con,
        args.output,
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        date_from=args.date_from,
        date_to=args.date_to,
        horizon=args.horizon,
        target_atr=args.target_atr,
        stop_atr=args.stop_atr,
        fmt=args.format,
        n_splits=args.n_splits,
        embargo_bars=args.embargo_bars,
        holdout_fraction=args.holdout_fraction,
    )
    con.close()
    print(f"Wrote {count} rows to {args.output}")


if __name__ == "__main__":
    main()
