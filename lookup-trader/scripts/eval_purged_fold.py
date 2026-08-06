#!/usr/bin/env python3
"""Print purged fold sizes and verify zero leakage."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.duck import register_features_view  # noqa: E402
from app.services.purged_cv import (  # noqa: E402
    assert_no_leakage,
    purged_kfold,
    timeframe_delta,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate purged CV fold sizes")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--horizon", type=int, default=max(settings.feature_horizons))
    parser.add_argument("--n-splits", type=int, default=5)
    parser.add_argument("--embargo-bars", type=int, default=None)
    args = parser.parse_args()

    con = duckdb.connect(":memory:")
    register_features_view(con)
    rows = con.execute(
        """
        SELECT ts FROM bar_features
        WHERE symbol = ? AND timeframe = ?
        ORDER BY ts
        """,
        [args.symbol.upper(), args.timeframe],
    ).fetchall()
    con.close()

    if not rows:
        raise SystemExit("No bar_features rows for symbol/timeframe")

    timestamps = [r[0] for r in rows]
    folds = purged_kfold(
        timestamps,
        horizon=args.horizon,
        n_splits=args.n_splits,
        embargo_bars=args.embargo_bars,
        bar_delta=timeframe_delta(args.timeframe),
    )
    assert_no_leakage(
        timestamps,
        folds,
        horizon=max(args.horizon, max(settings.feature_horizons)),
        bar_delta=timeframe_delta(args.timeframe),
    )

    for i, fold in enumerate(folds, start=1):
        print(
            f"fold {i}: train={len(fold.train_idx)} test={len(fold.test_idx)} "
            f"test_range={timestamps[int(fold.test_idx[0])]} → {timestamps[int(fold.test_idx[-1])]}"
        )
    print("leakage check: ok")


if __name__ == "__main__":
    main()
