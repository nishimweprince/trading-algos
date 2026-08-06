"""Read-only health snapshot for candle, feature, and model data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.services.bar_features import tags_half


def _partition_value(path: Path, key: str) -> str | None:
    prefix = f"{key}="
    return next((part[len(prefix) :] for part in path.parts if part.startswith(prefix)), None)


def _latest_rows(root: Path, columns: list[str]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for path in root.glob("**/month=*/part-*.parquet"):
        try:
            frame = pd.read_parquet(path, columns=columns)
        except (OSError, ValueError):
            continue
        if frame.empty:
            continue
        frame["symbol"] = frame.get("symbol", _partition_value(path, "symbol"))
        frame["timeframe"] = frame.get("timeframe", _partition_value(path, "timeframe"))
        rows.append(frame.sort_values("ts").tail(1))
    if not rows:
        return pd.DataFrame(columns=[*columns, "symbol", "timeframe"])
    result = pd.concat(rows, ignore_index=True)
    result["ts"] = pd.to_datetime(result["ts"], utc=True)
    return result.sort_values("ts")


def _model_health() -> dict[str, Any]:
    version = settings.outcome_artifact_version
    path = settings.outcome_artifact_root / version
    required = ["model.joblib", "metadata.json", "metrics.json", "dataset_manifest.json"]
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        return {
            "configured_version": version,
            "path": str(path),
            "status": "missing",
            "missing_files": missing,
        }
    try:
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("artifact_version") != version:
            raise ValueError("metadata artifact_version does not match configured version")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "configured_version": version,
            "path": str(path),
            "status": "invalid",
            "detail": str(exc),
        }
    return {
        "configured_version": version,
        "path": str(path),
        "status": "ready",
        "model_version": metadata.get("outcome_feature_version"),
    }


def _tag_parity(feature_rows: pd.DataFrame) -> dict[str, Any]:
    requested = settings.health_parity_sample_size
    required = {"ts", "symbol", "timeframe", "bar_tags", "atr_at_bar"}
    if feature_rows.empty or not required.issubset(feature_rows.columns):
        return {"status": "unavailable", "sampled": 0, "matched": 0, "requested": requested}

    sampled = feature_rows.sort_values("ts").tail(requested)
    matched = checked = 0
    errors: list[str] = []
    candle_root = settings.data_dir / "candles"
    for row in sampled.itertuples(index=False):
        paths = (
            candle_root
            / f"symbol={row.symbol}"
            / f"timeframe={row.timeframe}"
        ).glob("year=*/month=*/part-*.parquet")
        frames = [pd.read_parquet(path) for path in paths]
        if not frames:
            errors.append(f"{row.symbol}/{row.timeframe}: candles missing")
            continue
        candles = pd.concat(frames, ignore_index=True)
        candles["ts"] = pd.to_datetime(candles["ts"], utc=True)
        window = candles[candles["ts"] <= row.ts].sort_values("ts").tail(settings.warmup_bars)
        if window.empty:
            errors.append(f"{row.symbol}/{row.timeframe}/{row.ts}: window missing")
            continue
        checked += 1
        live = tags_half(window, float(row.atr_at_bar or 0.0))["bar_tags"]
        stored = json.loads(row.bar_tags) if isinstance(row.bar_tags, str) else row.bar_tags
        matched += int(live == stored)

    status = "ok" if checked == matched and checked else "mismatch" if checked else "unavailable"
    return {
        "status": status,
        "sampled": checked,
        "matched": matched,
        "requested": requested,
        "errors": errors[:5],
    }


def data_model_health(now: datetime | None = None) -> dict[str, Any]:
    """Build a health report without refreshing views or changing any state."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    candles = _latest_rows(settings.data_dir / "candles", ["ts"])
    latest_candle = candles.iloc[-1] if not candles.empty else None

    feature_columns = [
        "ts",
        "feature_version",
        "bar_feature_version",
        "bar_tags",
        "atr_at_bar",
    ]
    features = _latest_rows(settings.features_dir, feature_columns)
    latest_feature = features.iloc[-1] if not features.empty else None

    latest_ts = latest_candle["ts"] if latest_candle is not None else None
    lag_seconds = (
        max(0.0, (now - latest_ts.to_pydatetime()).total_seconds())
        if latest_ts is not None
        else None
    )
    return {
        "status": "ok" if latest_candle is not None else "degraded",
        "candles": {
            "latest_complete_candle": latest_ts.isoformat() if latest_ts is not None else None,
            "symbol": latest_candle["symbol"] if latest_candle is not None else None,
            "timeframe": latest_candle["timeframe"] if latest_candle is not None else None,
            "lag_seconds": lag_seconds,
        },
        "features": {
            "latest_timestamp": (
                latest_feature["ts"].isoformat() if latest_feature is not None else None
            ),
            "feature_version": (
                latest_feature["feature_version"] if latest_feature is not None else None
            ),
            "bar_feature_version": (
                latest_feature["bar_feature_version"] if latest_feature is not None else None
            ),
        },
        "model": _model_health(),
        "tag_parity": _tag_parity(features),
    }
