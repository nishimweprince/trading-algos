"""Export bar_features to a flat training matrix with committed y_outcome."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from app.config import settings
from app.services.base_rate import outcome_expr
from app.services.bar_features import level_key


def _flatten_level_touch(df: pd.DataFrame) -> pd.DataFrame:
    if "level_touch" not in df.columns:
        return df

    parsed = df["level_touch"].map(lambda v: json.loads(v) if isinstance(v, str) else v)
    for level in settings.touch_levels:
        key = level_key(level)
        for direction in ("up", "down"):
            col = f"touch_{key}_{direction}"
            df[col] = parsed.map(
                lambda v, k=key, d=direction: (
                    v.get(k, {}).get(d) if isinstance(v, dict) else None
                )
            )
    return df


def export_bar_features(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    *,
    symbol: str,
    timeframe: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    horizon: int = 24,
    target_atr: float = 1.5,
    stop_atr: float = 1.0,
    side: int = 1,
    fmt: str = "parquet",
    exclude_shape: bool = True,
) -> int:
    """Read partitioned parquet via DuckDB and write a flat training table."""
    glob = str(settings.features_dir / "**" / "part-*.parquet")
    conditions = ["symbol = ?", "timeframe = ?", "bar_feature_version = ?"]
    params: list = [symbol.upper(), timeframe, settings.bar_feature_version]
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    outcome = outcome_expr(horizon, target_atr, stop_atr, side)
    df = con.execute(
        f"""
        SELECT *, ({outcome}) AS y_outcome
        FROM read_parquet('{glob}', hive_partitioning=true)
        WHERE {where}
        ORDER BY ts
        """,
        params,
    ).fetchdf()

    if df.empty:
        return 0

    if exclude_shape:
        drop = [c for c in df.columns if c.startswith("shape_") or c == "fwd_shape"]
        df = df.drop(columns=drop, errors="ignore")

    df = _flatten_level_touch(df)

    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        df.to_csv(path, index=False)
    else:
        df.to_parquet(path, index=False)
    return len(df)
