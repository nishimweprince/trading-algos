"""Immutable weekly snapshots and automatic research-shadow rotation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss

from app.config import settings
from app.ml.meta.artifact import (
    load_meta_artifact,
    read_active_shadow,
    save_artifact,
    write_active_shadow,
)
from app.ml.meta.features import META_INPUT_FEATURES_V2
from app.ml.meta.shadow_artifacts import (
    FROZEN_CATBOOST_PARAMS,
    TARGET_TAKE_RATE,
    MetaShadowModel,
    derive_oof_take_threshold,
)
from app.ml.meta.training import CatBoostCandidate
from app.services.candle_quality import file_sha256
from app.services.meta_events_v2 import event_manifest_path_v2, event_path_v2
from app.services.meta_shadow_store import MetaShadowStore

MIN_FORWARD_EVENTS = 250
MIN_SELECTED_EVENTS = 25


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        frame.to_parquet(temporary, index=False)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_frame(store: MetaShadowStore) -> pd.DataFrame:
    historical = pd.read_parquet(event_path_v2())
    live_rows = []
    for event in store.resolved_training_events():
        row = {
            "event_id": event["event_id"],
            "symbol": event["symbol"],
            "timeframe": event["timeframe"],
            "signal_ts": event["signal_ts"],
            "side": event["side"],
            "primary_setup_id": event["primary_setup_id"],
            "setup_ids": json.dumps(event["setup_ids"], separators=(",", ":")),
            "confidence": event["confidence"],
            "entry_ts": event["entry_ts"],
            "exit_ts": event["exit_ts"],
            "net_r_3": event["net_r_3"],
            "net_r_5": event["net_r_5"],
            "net_r_8": event["net_r_8"],
            "y_meta": event["y_meta"],
            "meta_feature_version": 2,
            "meta_label_version": settings.meta_label_version,
            **event["causal_features_v2"],
        }
        live_rows.append(row)
    if live_rows:
        live = pd.DataFrame(live_rows)
        for name in historical.columns:
            if name not in live:
                live[name] = None
        live = live.loc[:, historical.columns]
        combined = pd.concat([historical, live], ignore_index=True)
    else:
        combined = historical
    return (
        combined.drop_duplicates("event_id", keep="last")
        .sort_values(["signal_ts", "side", "event_id"], kind="stable")
        .reset_index(drop=True)
    )


def create_training_snapshot(store: MetaShadowStore, cutoff: datetime) -> dict[str, Any]:
    frame = _snapshot_frame(store)
    frame["signal_ts"] = pd.to_datetime(frame["signal_ts"], utc=True)
    frame = frame[frame["signal_ts"] <= pd.Timestamp(cutoff)].reset_index(drop=True)
    identity = hashlib.sha256(
        pd.util.hash_pandas_object(
            frame[["event_id", "signal_ts", "y_meta", "net_r_8"]], index=False
        ).values.tobytes()
    ).hexdigest()
    root = settings.data_dir / "exports" / "meta_training_snapshots" / identity
    path = root / "events.parquet"
    manifest_path = root / "manifest.json"
    if not path.exists():
        _atomic_parquet(path, frame)
        manifest = {
            "snapshot_version": 1,
            "snapshot_sha256": identity,
            "rows": len(frame),
            "cutoff": pd.Timestamp(cutoff).isoformat(),
            "parquet_sha256": file_sha256(path),
            "historical_manifest_sha256": file_sha256(event_manifest_path_v2()),
            "model_feature_columns": list(META_INPUT_FEATURES_V2),
            "orders_enabled": False,
        }
        _atomic_json(manifest_path, manifest)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"frame": frame, "path": path, "manifest": manifest, "sha256": identity}


def _paired_log_loss_ci(y: np.ndarray, active: np.ndarray, challenger: np.ndarray) -> dict:
    values = -(y * np.log(challenger) + (1 - y) * np.log(1 - challenger)) + (
        y * np.log(active) + (1 - y) * np.log(1 - active)
    )
    rng = np.random.default_rng(0)
    block = max(1, min(50, len(values) // 10))
    starts_max = max(1, len(values) - block + 1)
    blocks = int(np.ceil(len(values) / block))
    means = []
    for _ in range(2000):
        starts = rng.integers(0, starts_max, size=blocks)
        sample = np.concatenate([values[start : start + block] for start in starts])[: len(values)]
        means.append(float(sample.mean()))
    return {
        "mean": float(values.mean()),
        "lo": float(np.percentile(means, 2.5)),
        "hi": float(np.percentile(means, 97.5)),
    }


def _fit_shadow_model(frame: pd.DataFrame, threshold: float) -> MetaShadowModel:
    candidate = CatBoostCandidate(
        FROZEN_CATBOOST_PARAMS, feature_columns=META_INPUT_FEATURES_V2
    ).fit(frame, frame["y_meta"])
    return MetaShadowModel(candidate, META_INPUT_FEATURES_V2, threshold)


def _new_version(now: datetime) -> str:
    return f"xauusd-h1-meta-v2-shadow-{now.strftime('%Y%m%dT%H%M%SZ')}"


def evaluate_weekly_shadow(
    store: MetaShadowStore,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if not force and not (now.weekday() == 5 and now.hour >= 12):
        report = {"status": "not_due", "evaluated_at": now.isoformat()}
        store.record_promotion(report)
        return report
    pointer = read_active_shadow()
    if not pointer:
        raise RuntimeError("Active meta shadow pointer is unavailable")
    snapshot = create_training_snapshot(store, now)
    previous_hash = store.state("last_training_snapshot_sha256")
    if previous_hash == snapshot["sha256"]:
        report = {
            "status": "no_change",
            "evaluated_at": now.isoformat(),
            "snapshot_sha256": snapshot["sha256"],
            "active_version": pointer["active_version"],
            "challenger_version": pointer.get("challenger_version"),
        }
        store.record_promotion(report)
        return report

    challenger_version = pointer.get("challenger_version")
    active_version = pointer["active_version"]
    if not challenger_version or challenger_version == active_version:
        version = _new_version(now)
        threshold_contract = derive_oof_take_threshold(
            snapshot["frame"], META_INPUT_FEATURES_V2
        )
        threshold = float(threshold_contract["threshold"])
        model = _fit_shadow_model(snapshot["frame"], threshold)
        metadata = {
            "model_kind": "binary_meta_catboost",
            "model_version": "meta-shadow-v1",
            "status": "research_shadow_challenger",
            "promoted": False,
            "orders_enabled": False,
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "feature_columns": list(META_INPUT_FEATURES_V2),
            "schema_sha256": snapshot["manifest"]["parquet_sha256"],
            "bar_feature_version": settings.bar_feature_version,
            "meta_feature_version": 2,
            "meta_label_version": settings.meta_label_version,
            "threshold": threshold,
            "target_take_rate": TARGET_TAKE_RATE,
            "realized_oof_take_rate": threshold_contract["realized_oof_take_rate"],
            "training_rows": len(snapshot["frame"]),
            "training_cutoff": now.isoformat(),
            "created_at": now.isoformat(),
            "catboost_params": FROZEN_CATBOOST_PARAMS,
        }
        save_artifact(
            settings.meta_artifact_root,
            version,
            model=model,
            metadata=metadata,
            metrics={"status": "awaiting_forward_evidence"},
            dataset_manifest=snapshot["manifest"],
        )
        write_active_shadow(
            {
                **pointer,
                "challenger_version": version,
                "challenger_started_at": now.isoformat(),
            }
        )
        store.set_state("last_training_snapshot_sha256", snapshot["sha256"])
        report = {
            "status": "challenger_created",
            "snapshot_sha256": snapshot["sha256"],
            "active_version": active_version,
            "challenger_version": version,
            "orders_enabled": False,
        }
        store.record_promotion(report)
        return report

    since = datetime.fromisoformat(pointer.get("challenger_started_at", pointer["activated_at"]))
    rows = store.paired_evaluation(
        active_version=active_version,
        challenger_version=challenger_version,
        since=since,
    )
    if len(rows) < MIN_FORWARD_EVENTS:
        report = {
            "status": "insufficient_forward_evidence",
            "snapshot_sha256": snapshot["sha256"],
            "active_version": active_version,
            "challenger_version": challenger_version,
            "resolved_forward_events": len(rows),
            "required": MIN_FORWARD_EVENTS,
        }
        store.set_state("last_training_snapshot_sha256", snapshot["sha256"])
        store.record_promotion(report)
        return report

    evaluation = pd.DataFrame(rows)
    y = evaluation["y_meta"].to_numpy(dtype=int)
    active_p = evaluation["active_probability"].to_numpy(dtype=float)
    challenger_p = evaluation["challenger_probability"].to_numpy(dtype=float)
    active_take = active_p >= evaluation["active_threshold"].to_numpy(dtype=float)
    challenger_take = challenger_p >= evaluation["challenger_threshold"].to_numpy(dtype=float)
    selected = int(challenger_take.sum())
    active_selected_r = (
        float(evaluation.loc[active_take, "net_r_8"].mean()) if active_take.any() else 0.0
    )
    challenger_selected_r = (
        float(evaluation.loc[challenger_take, "net_r_8"].mean()) if challenger_take.any() else 0.0
    )
    ci = _paired_log_loss_ci(y, active_p, challenger_p)
    months = pd.to_datetime(evaluation["signal_ts"], utc=True).dt.to_period("M")
    contribution = (
        evaluation.assign(_month=months, _take=challenger_take)
        .groupby("_month")
        .apply(
            lambda group: (
                float(group.loc[group["_take"], "net_r_8"].sum()) if group["_take"].any() else 0.0
            ),
            include_groups=False,
        )
    )
    positive = contribution[contribution > 0]
    max_month_share = float(positive.max() / positive.sum()) if len(positive) else 1.0
    challenger_model, challenger_metadata = load_meta_artifact(challenger_version)
    parity_event = store.event_by_id(str(evaluation.iloc[0]["event_id"]))
    if parity_event is None or parity_event["causal_features_v2"] is None:
        parity_ok = False
    else:
        parity_probability = float(
            challenger_model.predict_proba(pd.DataFrame([parity_event["causal_features_v2"]]))[0]
        )
        parity_ok = bool(
            np.isclose(
                parity_probability,
                float(evaluation.iloc[0]["challenger_probability"]),
                rtol=0,
                atol=1e-12,
            )
        )
    gates = {
        "minimum_forward_events": len(evaluation) >= MIN_FORWARD_EVENTS,
        "minimum_selected_events": selected >= MIN_SELECTED_EVENTS,
        "paired_log_loss_upper_below_zero": ci["hi"] < 0,
        "brier_no_worse": brier_score_loss(y, challenger_p) <= brier_score_loss(y, active_p),
        "positive_net_r_8_lift": challenger_selected_r > active_selected_r,
        "both_sides_represented": evaluation.loc[challenger_take, "side"].nunique() == 2,
        "no_month_over_half_positive_lift": max_month_share <= 0.5,
        "artifact_contract_compatible": (
            challenger_metadata.get("orders_enabled") is False
            and tuple(challenger_metadata.get("feature_columns", ())) == META_INPUT_FEATURES_V2
            and challenger_metadata.get("meta_feature_version") == 2
            and challenger_metadata.get("meta_label_version") == settings.meta_label_version
            # Alert selectivity is part of the contract. A challenger built to a
            # different take rate is a different strategy, and comparing its
            # selected net R against the active one would be comparing volumes.
            and challenger_metadata.get("target_take_rate") == TARGET_TAKE_RATE
        ),
        "batch_live_prediction_parity": parity_ok,
    }
    passed = all(gates.values())
    report = {
        "status": "promoted" if passed else "rejected",
        "snapshot_sha256": snapshot["sha256"],
        "active_version": active_version,
        "challenger_version": challenger_version,
        "events": len(evaluation),
        "challenger_selected": selected,
        "active_log_loss": float(log_loss(y, active_p, labels=[0, 1])),
        "challenger_log_loss": float(log_loss(y, challenger_p, labels=[0, 1])),
        "paired_log_loss_difference_ci": ci,
        "active_brier": float(brier_score_loss(y, active_p)),
        "challenger_brier": float(brier_score_loss(y, challenger_p)),
        "active_selected_net_r_8": active_selected_r,
        "challenger_selected_net_r_8": challenger_selected_r,
        "max_positive_month_share": max_month_share,
        "gates": gates,
        "orders_enabled": False,
    }
    if passed:
        promoted_version = f"{_new_version(now)}-promoted"
        threshold = float(challenger_metadata["threshold"])
        promoted_model = _fit_shadow_model(snapshot["frame"], threshold)
        metadata = {
            **challenger_metadata,
            "status": "research_shadow",
            "promoted": True,
            "orders_enabled": False,
            "training_rows": len(snapshot["frame"]),
            "training_cutoff": now.isoformat(),
            "created_at": now.isoformat(),
        }
        metadata.pop("artifact_version", None)
        metadata.pop("model_sha256", None)
        save_artifact(
            settings.meta_artifact_root,
            promoted_version,
            model=promoted_model,
            metadata=metadata,
            metrics=report,
            dataset_manifest=snapshot["manifest"],
        )
        loaded, loaded_metadata = load_meta_artifact(promoted_version)
        probability = float(loaded.predict_proba(snapshot["frame"].tail(1))[0])
        if not 0 <= probability <= 1 or loaded_metadata.get("orders_enabled") is not False:
            raise RuntimeError("Challenger canary failed")
        report["promoted_version"] = promoted_version
        write_active_shadow(
            {
                **pointer,
                "previous_active_version": active_version,
                "active_version": promoted_version,
                "reference_version": promoted_version,
                "challenger_version": None,
                "activated_at": now.isoformat(),
                "challenger_started_at": None,
                "orders_enabled": False,
            }
        )
    else:
        write_active_shadow({**pointer, "challenger_version": None, "challenger_started_at": None})
    store.set_state("last_training_snapshot_sha256", snapshot["sha256"])
    store.record_promotion(report)
    return report
