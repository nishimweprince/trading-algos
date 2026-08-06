"""Read-only health snapshot for candle, feature, and model data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.services.bar_features import tags_half
from app.services.bar_features import htf_context
from app.services.pips import pip_size
from app.services.shadow_store import ShadowStore
from app.ml.outcome.infer import infer_outcomes


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


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
        metrics = json.loads((path / "metrics.json").read_text(encoding="utf-8"))
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
        "model_version": version,
        "outcome_feature_version": metadata.get("outcome_feature_version"),
        "promoted": False,
        "holdout": {
            "model_log_loss": metrics.get("holdout", {}).get("model", {}).get(
                "multiclass_log_loss"
            ),
            "baseline_log_loss": metrics.get("holdout", {}).get(
                "context_frequency_baseline", {}
            ).get("multiclass_log_loss"),
            "model_brier": metrics.get("holdout", {}).get("model", {}).get(
                "multiclass_brier"
            ),
            "baseline_brier": metrics.get("holdout", {}).get(
                "context_frequency_baseline", {}
            ).get("multiclass_brier"),
            "ece": metrics.get("holdout", {}).get("model", {}).get("reliability", {}).get(
                "ece"
            ),
        },
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


def _shadow_inference_parity() -> dict[str, Any]:
    store = ShadowStore(settings.shadow_db_path)
    rows = store.latest_predictions(artifact_version=settings.outcome_artifact_version)
    if len(rows) != 2:
        return {"status": "unavailable", "checked": 0, "matched": 0}
    try:
        ts = pd.Timestamp(rows[0]["ts"])
        candle_root = settings.data_dir / "candles"

        def load(timeframe: str) -> pd.DataFrame:
            paths = sorted(
                (candle_root / "symbol=XAUUSD" / f"timeframe={timeframe}").glob(
                    "year=*/month=*/part-*.parquet"
                )
            )
            frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
            return frame.sort_values("ts").drop_duplicates("ts", keep="last")

        h1 = load("H1")
        h4 = load("H4")
        history = h1[h1["ts"] <= ts].tail(settings.warmup_bars)
        higher = h4[h4["ts"] <= ts].tail(settings.warmup_bars)
        context = htf_context(higher) if not higher.empty else None
        current = infer_outcomes(
            history,
            "XAUUSD",
            "H1",
            pip_size("XAUUSD"),
            context,
            artifact_version=settings.outcome_artifact_version,
        )
        expected = {1: current.long, -1: current.short}
        matched = 0
        for row in rows:
            direction = expected[int(row["side"])]
            matched += int(
                all(
                    abs(float(row[name]) - getattr(direction, name)) <= 1e-12
                    for name in ("p_win", "p_loss", "p_timeout")
                )
            )
        return {
            "status": "ok" if matched == 2 else "mismatch",
            "checked": 2,
            "matched": matched,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "checked": 0,
            "matched": 0,
            "detail": type(exc).__name__,
        }


def data_model_health(now: datetime | None = None) -> dict[str, Any]:
    """Build a health report without refreshing views or changing any state."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    candles = _latest_rows(settings.data_dir / "candles", ["ts"])
    latest_candle = candles.iloc[-1] if not candles.empty else None

    def latest(timeframe: str):
        rows = candles[
            (candles["symbol"] == "XAUUSD") & (candles["timeframe"] == timeframe)
        ]
        return rows.iloc[-1] if not rows.empty else None

    latest_h1 = latest("H1")
    latest_h4 = latest("H4")

    feature_columns = [
        "ts",
        "feature_version",
        "bar_feature_version",
        "bar_tags",
        "atr_at_bar",
    ]
    features = _latest_rows(settings.features_dir, feature_columns)
    latest_feature = features.iloc[-1] if not features.empty else None

    latest_ts = latest_h1["ts"] if latest_h1 is not None else None
    lag_seconds = (
        max(0.0, (now - latest_ts.to_pydatetime()).total_seconds())
        if latest_ts is not None
        else None
    )
    boundary = _read_json(settings.data_dir / "candle_sources" / "capital_boundary.json")
    publication = _read_json(settings.data_dir / "candle_sources" / "capital_publish.json")
    quarantine_root = settings.data_dir / "quarantine" / "capital-conflicts"
    quarantine_count = (
        len(list(quarantine_root.glob("*.parquet"))) if quarantine_root.exists() else 0
    )
    shadow = ShadowStore(settings.shadow_db_path).status()
    capital_configured = all(
        (
            settings.capital_api_key,
            settings.capital_identifier,
            settings.capital_api_password,
            settings.capital_epic,
        )
    )
    tag_parity = _tag_parity(features)
    inference_parity = _shadow_inference_parity()
    return {
        "status": "ok" if latest_candle is not None else "degraded",
        "candles": {
            "latest_complete_candle": latest_ts.isoformat() if latest_ts is not None else None,
            "latest_closed_h1": latest_ts.isoformat() if latest_ts is not None else None,
            "latest_derived_h4": (
                latest_h4["ts"].isoformat() if latest_h4 is not None else None
            ),
            "symbol": "XAUUSD" if latest_h1 is not None else None,
            "timeframe": "H1" if latest_h1 is not None else None,
            "lag_seconds": lag_seconds,
        },
        "capital": {
            "configured": capital_configured,
            "environment": settings.capital_environment,
            "epic": settings.capital_epic,
            "price_side": settings.capital_price_side,
            "session_status": shadow.get("status") if capital_configured else "not_configured",
            "request_status": (
                shadow.get("status")
                if shadow.get("status") in {"ok", "error"}
                else publication.get("request_status") if publication else None
            ),
            "server_time": publication.get("capital_server_time") if publication else None,
            "feed_lag_seconds": lag_seconds,
            "unexpected_gaps": publication.get("unexpected_gaps") if publication else None,
            "quarantines": quarantine_count,
            "last_worker_result": shadow.get("last_run"),
        },
        "source_boundary": boundary,
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
        "training_live_sources": {
            "training": "histdata",
            "live": "capital",
            "mismatch": True,
            "expected_by_contract": True,
        },
        "parity": {
            "store_live_tags": tag_parity,
            "batch_shadow_inference": inference_parity,
        },
        "tag_parity": tag_parity,
        "shadow": shadow,
    }
