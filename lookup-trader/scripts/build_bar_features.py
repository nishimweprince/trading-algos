#!/usr/bin/env python3
"""Precompute one feature row per closed candle into hive-partitioned Parquet.

The store is a build artifact, not operational state: everything in it is derived
from data/candles, so a changed threshold is a rebuild rather than a migration.
Rows carry `bar_feature_version` and the builder rewrites any row whose version no
longer matches.

Incremental by default. Two kinds of row get rebuilt on a re-run: rows that were
written before the forward window had fully elapsed (the same deferred resolution
`resolve_pending_occurrences` does for live signals), and rows from an older
feature version.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.duck import register_candles_view  # noqa: E402
from app.services import bar_features as bf  # noqa: E402
from app.services.pips import pip_size  # noqa: E402
from app.utils.parquet import month_partition_path, write_month_partition  # noqa: E402
from app.utils.time import to_utc_series  # noqa: E402

# Written into the parquet file. symbol/timeframe/year/month are carried by the
# hive path instead — duplicating them in the file makes read_parquet emit two
# columns of the same name.
_PARTITION_COLUMNS = ["symbol", "timeframe", "year", "month"]

_STRING_COLUMNS = {
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "htf_atr_bucket",
    "context_fingerprint",
    "bar_feature_version",
    "feature_version",
    "level_touch",
}

_LIST_COLUMNS = {"shape_480", "shape_48", "fwd_shape"}


def _int_columns() -> set[str]:
    cols = {
        "warmup_bars_available",
        "back_bars_available",
        "bar_in_session",
        "bars_since_swing_high",
        "bars_since_swing_low",
    }
    for h in settings.feature_horizons:
        cols |= {f"fwd{h}_bars_to_max", f"fwd{h}_bars_to_min", f"fwd{h}_bars_available"}
    return cols


def _bool_columns() -> set[str]:
    cols = {"context_reliable", "session_overlap"}
    for h in settings.feature_horizons:
        cols |= {f"fwd{h}_complete", f"fwd{h}_max_first"}
    return cols


def _coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Pin every column to one type.

    Without this a month in which some feature is always null writes a
    null-typed parquet column, and the view over all months then has to reconcile
    NULL against DOUBLE.
    """
    ints, bools = _int_columns(), _bool_columns()
    for col in df.columns:
        if col in _LIST_COLUMNS or col == "ts":
            continue
        if col in _STRING_COLUMNS:
            df[col] = df[col].astype("object")
        elif col in bools:
            df[col] = df[col].astype("boolean")
        elif col in ints:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int32")
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


def _load_candles(con, symbol: str, timeframe: str) -> pd.DataFrame:
    df = con.execute(
        """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY ts ASC
        """,
        [symbol, timeframe],
    ).df()
    # Candles are TIMESTAMP WITH TIME ZONE; pandas renders them locally. Every
    # hour, date and partition key downstream is UTC, so convert at the boundary.
    df["ts"] = to_utc_series(df["ts"])
    # The candles view unions a legacy year-only glob with the month partitions,
    # so a bar written under both layouts arrives twice. A duplicate would shift
    # every warmup window past it by a bar and emit two rows for one candle.
    return df.drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)


def _htf_frame(con, symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Higher-timeframe context per HTF bar, ready to join onto the LTF series.

    Computed once per HTF bar rather than once per LTF bar: `compute_htf_trend_state`
    issues a query each call, which is fine for a single signal and ruinous across
    a whole series.
    """
    htf = settings.htf_map.get(timeframe)
    if not htf:
        return None

    candles = _load_candles(con, symbol, htf)
    if candles.empty:
        return None

    rows = []
    for idx in range(len(candles)):
        window = candles.iloc[max(0, idx - settings.warmup_bars + 1) : idx + 1]
        ctx = bf.htf_context(window)
        rows.append({"ts": candles["ts"].iloc[idx], **ctx})

    frame = pd.DataFrame(rows)
    return frame.rename(
        columns={
            "trend_state": "htf_trend_state",
            "atr_bucket": "htf_atr_bucket",
            "range_pct": "htf_range_pct",
        }
    )


def _join_htf(candles: pd.DataFrame, htf: pd.DataFrame | None) -> pd.DataFrame:
    """Attach the last HTF bar that had closed at each LTF bar's close.

    `fetch_labeling_window` selects the HTF bar with `ts <= signal_ts`, so a
    backward as-of join reproduces the live pairing exactly.
    """
    cols = ["htf_trend_state", "htf_atr_bucket", "htf_range_pct"]
    if htf is None or htf.empty:
        for col in cols:
            candles[col] = None
        return candles

    return pd.merge_asof(
        candles.sort_values("ts"),
        htf.sort_values("ts"),
        on="ts",
        direction="backward",
    )


def _existing_index(root: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    """ts -> version and forward-completeness for rows already in the store."""
    base = root / f"symbol={symbol}" / f"timeframe={timeframe}"
    files = sorted(base.glob("**/part-*.parquet"))
    if not files:
        return pd.DataFrame(columns=["ts", "bar_feature_version", "complete"])

    longest = max(settings.feature_horizons)
    frames = []
    for path in files:
        frame = pd.read_parquet(path, columns=["ts", "bar_feature_version", f"fwd{longest}_complete"])
        frames.append(frame.rename(columns={f"fwd{longest}_complete": "complete"}))
    return pd.concat(frames, ignore_index=True)


def _bars_to_build(
    candles: pd.DataFrame,
    existing: pd.DataFrame,
    min_warmup: int,
    since: datetime | None,
    rebuild: bool,
) -> list[int]:
    eligible = [i for i in range(len(candles)) if i + 1 >= min_warmup]
    if since is not None:
        since_ts = pd.Timestamp(since)
        if since_ts.tzinfo is not None:
            since_ts = since_ts.tz_localize(None)
        eligible = [i for i in eligible if candles["ts"].iloc[i] >= since_ts]
    if rebuild or existing.empty:
        return eligible

    fresh = existing.loc[
        (existing["bar_feature_version"] == settings.bar_feature_version)
        & (existing["complete"].fillna(False).astype(bool))
    ]
    done = set(fresh["ts"])
    return [i for i in eligible if candles["ts"].iloc[i] not in done]


def build(
    symbol: str,
    timeframe: str,
    output_dir: Path,
    min_warmup: int,
    since: datetime | None = None,
    rebuild: bool = False,
    dry_run: bool = False,
) -> int:
    # In-memory: the builder reads the candle parquet and writes feature parquet,
    # so it never needs engine.duckdb — and taking that file's exclusive lock
    # would mean the build could not run while the API is up.
    con = duckdb.connect(":memory:")
    register_candles_view(con, force=True)
    try:
        candles = _load_candles(con, symbol, timeframe)
        if candles.empty:
            raise SystemExit(f"No candles for {symbol} {timeframe}")
        candles = _join_htf(candles, _htf_frame(con, symbol, timeframe))
    finally:
        con.close()

    existing = _existing_index(output_dir, symbol, timeframe)
    targets = _bars_to_build(candles, existing, min_warmup, since, rebuild)
    if not targets:
        print(f"{symbol} {timeframe}: nothing to build ({len(existing)} rows already current)")
        return 0

    print(
        f"{symbol} {timeframe}: {len(candles)} bars, building {len(targets)} "
        f"({candles['ts'].iloc[targets[0]]} → {candles['ts'].iloc[targets[-1]]})"
    )

    pip = pip_size(symbol)
    depth = bf.max_horizon()
    ohlc = candles[["ts", "open", "high", "low", "close", "volume"]]

    rows = []
    for n, idx in enumerate(targets, start=1):
        window = ohlc.iloc[max(0, idx - settings.warmup_bars + 1) : idx + 1]
        forward = ohlc.iloc[idx + 1 : idx + 1 + depth]
        htf = {
            "trend_state": candles["htf_trend_state"].iloc[idx],
            "atr_bucket": candles["htf_atr_bucket"].iloc[idx],
            "range_pct": candles["htf_range_pct"].iloc[idx],
        }
        row = bf.compute_bar_row(window, forward, symbol, timeframe, pip, htf)
        row["level_touch"] = json.dumps(row["level_touch"], separators=(",", ":"))
        rows.append(row)
        if n % 500 == 0:
            print(f"  {n}/{len(targets)}")

    frame = _coerce_dtypes(pd.DataFrame(rows))
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame["year"] = frame["ts"].dt.year
    frame["month"] = frame["ts"].dt.month
    columns = [c for c in frame.columns if c not in _PARTITION_COLUMNS]

    if dry_run:
        print(frame[["ts", "trend_state", "atr_bucket", "fwd24_max_atr", "fwd24_complete"]].head())
        return len(frame)

    written = 0
    for (year, month), group in frame.groupby(["year", "month"]):
        out_path = month_partition_path(output_dir, symbol, timeframe, int(year), int(month))
        count = write_month_partition(out_path, group, columns)
        print(f"Wrote {out_path} ({count} rows)")
        written += len(group)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the per-bar feature store")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--output-dir", type=Path, default=settings.features_dir)
    parser.add_argument(
        "--min-warmup",
        type=int,
        default=settings.ema_period,
        help="Skip bars with fewer preceding bars; their context is never usable.",
    )
    parser.add_argument("--from", dest="since", type=datetime.fromisoformat, default=None)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild every eligible bar.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    build(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe.upper(),
        output_dir=args.output_dir,
        min_warmup=args.min_warmup,
        since=args.since,
        rebuild=args.rebuild,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
