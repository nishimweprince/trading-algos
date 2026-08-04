"""Flatten occurrences to a training-ready table."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from app.services.occurrences import JSON_COLUMNS


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """Expand the JSON columns into prefixed scalar columns.

    r_grid becomes one column per target multiple holding just the outcome —
    the bar count stays in the JSON, which the parquet export keeps intact.
    """
    for column in JSON_COLUMNS:
        if column not in df.columns:
            continue
        parsed = df[column].map(lambda v: json.loads(v) if isinstance(v, str) else v)
        keys: list[str] = []
        for value in parsed:
            if isinstance(value, dict):
                keys.extend(k for k in value if k not in keys)
        for key in keys:
            out_name = f"{column}_{key}"
            values = parsed.map(lambda v, k=key: v.get(k) if isinstance(v, dict) else None)
            if column == "r_grid":
                values = values.map(lambda v: v.get("result") if isinstance(v, dict) else v)
            elif values.map(lambda v: isinstance(v, (dict, list))).any():
                values = values.map(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
            df[out_name] = values
    return df


def export_occurrences(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    fmt: str = "parquet",
    source: str | None = None,
    include_excluded: bool = False,
) -> int:
    conditions = []
    params: list = []
    if source:
        conditions.append("source = ?")
        params.append(source)
    if not include_excluded:
        conditions.append("excluded IS NOT TRUE")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    df = con.execute(f"SELECT * FROM occurrences {where} ORDER BY ts ASC", params).fetchdf()
    if df.empty:
        return 0

    df = _flatten(df)
    if fmt == "csv":
        # Keep the raw JSON alongside the flattened columns out of the CSV; it
        # renders as unquoted noise and the flattened columns already carry it.
        df.drop(columns=[c for c in JSON_COLUMNS if c in df.columns]).to_csv(path, index=False)
    else:
        for column in JSON_COLUMNS:
            if column in df.columns:
                df[column] = df[column].map(
                    lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v
                )
        df.to_parquet(path, index=False)
    return len(df)
