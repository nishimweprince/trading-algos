"""Build the leakage-proof v1 outcome-model dataset and manifest."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.config import settings
from app.ml.outcome.features import (
    MODEL_FEATURES,
    OUTCOME_FEATURE_VERSION,
    SETUP_IDS,
    causal_store_features,
    validate_causal_features,
)
from app.services.base_rate import outcome_expr
from app.services.purged_cv import chronological_walk_forward

CONTRACT_SYMBOL = "XAUUSD"
CONTRACT_TIMEFRAME = "H1"
CONTRACT_HORIZON = 24
CONTRACT_TARGET_ATR = 1.5
CONTRACT_STOP_ATR = 1.0
SIDES = (-1, 1)


def manifest_path(path: Path) -> Path:
    return path.with_suffix(".manifest.json")


def _validate_contract(
    symbol: str, timeframe: str, horizon: int, target_atr: float, stop_atr: float
) -> None:
    actual = (symbol.upper(), timeframe.upper(), horizon, target_atr, stop_atr)
    required = (
        CONTRACT_SYMBOL,
        CONTRACT_TIMEFRAME,
        CONTRACT_HORIZON,
        CONTRACT_TARGET_ATR,
        CONTRACT_STOP_ATR,
    )
    if actual != required:
        raise ValueError(
            "outcome-v1 requires XAUUSD H1, horizon=24, target=1.5 ATR, stop=1 ATR"
        )


def _parse_tags(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not isinstance(value.get("tags", []), list):
        raise ValueError("bar_tags must be deterministic TagResult JSON")
    return value.get("tags", [])


def _encode_tags(df: pd.DataFrame) -> pd.DataFrame:
    known = set(SETUP_IDS)
    encoded = {
        name: [0.0] * len(df)
        for name in MODEL_FEATURES
        if name.startswith("tag_")
    }
    for row_number, raw in enumerate(df.pop("bar_tags")):
        for tag in _parse_tags(raw):
            setup_id = str(tag.get("setup_id", ""))
            if setup_id not in known:
                raise ValueError(f"Unknown setup_id in bar_tags: {setup_id!r}")
            state = tag.get("state")
            if state not in {"complete", "forming", "invalidated"}:
                raise ValueError(f"Unknown tag state: {state!r}")
            if state == "invalidated":
                continue
            quality = float(tag.get("confidence", 0.0))
            encoded[f"tag_{state}__{setup_id}"][row_number] = 1.0
            quality_col = f"tag_match_quality__{setup_id}"
            encoded[quality_col][row_number] = max(encoded[quality_col][row_number], quality)
    return pd.concat([df, pd.DataFrame(encoded, index=df.index)], axis=1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    source_root = (
        settings.features_dir / f"symbol={symbol.upper()}" / f"timeframe={timeframe.upper()}"
    )
    files = sorted(source_root.glob("**/part-*.parquet"))
    return [
        {
            "path": str(path.relative_to(settings.features_dir)),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]


def _iso(value: Any) -> str:
    return pd.Timestamp(value).isoformat()


def _range(df: pd.DataFrame) -> dict[str, str] | None:
    if df.empty:
        return None
    return {"min": _iso(df["ts"].min()), "max": _iso(df["ts"].max())}


def _fold_manifest(df: pd.DataFrame, plan) -> list[dict[str, Any]]:
    rows = []
    for number, fold in enumerate(plan.folds, start=1):
        train = df.iloc[fold.train_idx]
        test = df.iloc[fold.test_idx]
        rows.append(
            {
                "fold": number,
                "train_bars": len(train),
                "validation_bars": len(test),
                "train_range": _range(train),
                "validation_range": _range(test),
            }
        )
    return rows


def _schema_hash(df: pd.DataFrame) -> str:
    schema = [{"name": name, "dtype": str(df[name].dtype)} for name in df.columns]
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


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
    fmt: str = "parquet",
    n_splits: int = 5,
    embargo_bars: int | None = None,
    holdout_fraction: float = 0.2,
) -> int:
    """Write one causal feature row per bar and side plus a deterministic manifest."""
    _validate_contract(symbol, timeframe, horizon, target_atr, stop_atr)
    validate_causal_features(list(MODEL_FEATURES))
    glob = str(settings.features_dir / "**" / "part-*.parquet")
    conditions = [
        "symbol = ?",
        "timeframe = ?",
        "bar_feature_version = ?",
        "context_reliable IS TRUE",
        f"fwd{horizon}_complete IS TRUE",
    ]
    params: list = [symbol.upper(), timeframe.upper(), settings.bar_feature_version]
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    store_features = causal_store_features()
    select_features = ", ".join(store_features)
    long_outcome = outcome_expr(horizon, target_atr, stop_atr, 1)
    short_outcome = outcome_expr(horizon, target_atr, stop_atr, -1)
    df = con.execute(
        f"""
        SELECT symbol, timeframe, ts, feature_version, bar_feature_version,
               {select_features}, bar_tags,
               ({long_outcome}) AS _long_outcome,
               ({short_outcome}) AS _short_outcome
        FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)
        WHERE {where}
        ORDER BY ts
        """,
        params,
    ).fetchdf()

    if df.empty:
        return 0

    df = _encode_tags(df)
    feature_versions = sorted(str(value) for value in df["feature_version"].dropna().unique())
    if len(feature_versions) != 1:
        raise ValueError(f"Expected one feature_version, found {feature_versions}")

    timestamps = [pd.Timestamp(value).to_pydatetime() for value in df["ts"]]
    plan = chronological_walk_forward(
        timestamps,
        timeframe=timeframe,
        horizon=max(settings.feature_horizons),
        n_splits=n_splits,
        embargo_bars=embargo_bars,
        holdout_fraction=holdout_fraction,
    )
    development_end = int(plan.holdout_idx[0]) - plan.embargo_bars
    bars = pd.concat(
        [
            df.iloc[:development_end].assign(dataset_partition="development"),
            df.iloc[plan.holdout_idx].assign(dataset_partition="holdout"),
        ],
        ignore_index=True,
    )
    side_frames = []
    for side in SIDES:
        outcome_column = "_long_outcome" if side == 1 else "_short_outcome"
        frame = bars.drop(columns=["_long_outcome", "_short_outcome"]).copy()
        frame.insert(3, "side", side)
        frame["y_outcome"] = bars[outcome_column]
        side_frames.append(frame)
    output = pd.concat(side_frames, ignore_index=True).sort_values(
        ["ts", "side"], kind="stable", ignore_index=True
    )
    ordered = [
        "symbol",
        "timeframe",
        "ts",
        "side",
        "dataset_partition",
        *MODEL_FEATURES,
        "y_outcome",
    ]
    output = output[ordered]

    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        output.to_csv(path, index=False)
        persisted_output = pd.read_csv(path)
    else:
        output.to_parquet(path, index=False)
        persisted_output = pd.read_parquet(path)

    holdout_bars = df.iloc[plan.holdout_idx]
    manifest = {
        "manifest_version": 1,
        "dataset_contract": "XAUUSD-H1-outcome-v1",
        "outcome_feature_version": OUTCOME_FEATURE_VERSION,
        "schema_sha256": _schema_hash(persisted_output),
        "columns": list(output.columns),
        "bar_feature_version": settings.bar_feature_version,
        "feature_version": feature_versions[0],
        "source": {
            "kind": "bar_features_parquet",
            "files": _source_identity(symbol, timeframe),
        },
        "label_policy": {
            "horizon_bars": horizon,
            "target_atr": target_atr,
            "stop_atr": stop_atr,
            "ambiguous_policy": settings.ambiguous_policy,
            "outcomes": ["win", "loss", "timeout"],
            "sides": list(SIDES),
        },
        "guards": {"complete": f"fwd{horizon}_complete IS TRUE", "context_reliable": True},
        "timestamp_ranges": {
            "source_complete_reliable": _range(df),
            "development": _range(df.iloc[:development_end]),
            "holdout": _range(holdout_bars),
        },
        "rows": len(output),
        "bars": len(output) // 2,
        "split_policy": {
            "kind": "chronological_expanding_walk_forward",
            "timeframe": timeframe.upper(),
            "bar_delta_seconds": int(plan.bar_delta.total_seconds()),
            "purge_bars": plan.purge_bars,
            "embargo_bars": plan.embargo_bars,
            "holdout_fraction": holdout_fraction,
            "folds": _fold_manifest(df.iloc[:development_end], plan),
        },
    }
    manifest_path(path).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return len(output)
