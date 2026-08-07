"""Deterministic calendar-enhanced meta-event v2 export.

V1 is a frozen source artifact. V2 pairs the exact same event IDs and labels
with causal schedule features; it never regenerates or overwrites v1.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.services.calendar.features import (
    CALENDAR_CAUSAL_COLUMNS,
    CALENDAR_MODEL_FEATURES,
    build_calendar_feature_frame,
)
from app.services.calendar.store import (
    calendar_manifest_path,
    coverage_parquet_path,
    events_parquet_path,
)
from app.services.candle_quality import file_sha256
from app.services.meta_events import META_MODEL_FEATURES, event_manifest_path, event_path

META_FEATURE_VERSION_V2 = 2
META_EVENT_MANIFEST_VERSION_V2 = 2
META_MODEL_FEATURES_V2 = (*META_MODEL_FEATURES, *CALENDAR_MODEL_FEATURES)


def event_path_v2() -> Path:
    return settings.data_dir / "exports" / "meta_events_v2.parquet"


def event_manifest_path_v2() -> Path:
    return settings.data_dir / "exports" / "meta_events_v2.manifest.json"


def event_report_path_v2(symbol: str, timeframe: str) -> Path:
    return settings.data_dir / "reports" / f"meta-events-{symbol}-{timeframe}-v2.json"


def _schema_sha256(frame: pd.DataFrame) -> str:
    schema = [{"name": name, "dtype": str(frame[name].dtype)} for name in frame.columns]
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


def _calendar_training_source_sha256(signal_times: pd.Series) -> str:
    """Hash only calendar rows capable of affecting the historical export.

    Daily live refreshes append future weeks to the operational store. Those
    unrelated rows must not rewrite a frozen historical training dataset.
    """
    timestamps = pd.to_datetime(signal_times, utc=True)
    date_from = (timestamps.min() - pd.Timedelta(days=8)).date()
    date_to = (timestamps.max() + pd.Timedelta(days=2)).date()
    events = pd.read_parquet(
        events_parquet_path(), columns=["source_event_id", *CALENDAR_CAUSAL_COLUMNS]
    )
    coverage = pd.read_parquet(coverage_parquet_path())
    events["event_date"] = pd.to_datetime(events["event_date"]).dt.date
    coverage["calendar_date"] = pd.to_datetime(coverage["calendar_date"]).dt.date
    events = events[(events["event_date"] >= date_from) & (events["event_date"] <= date_to)].copy()
    coverage = coverage[
        (coverage["calendar_date"] >= date_from) & (coverage["calendar_date"] <= date_to)
    ].copy()
    for frame in (events, coverage):
        for name in frame.columns:
            frame[name] = frame[name].astype("string").fillna("<null>")
    events = events.sort_values(["event_date", "time_utc", "source_event_id"])
    coverage = coverage.sort_values("calendar_date")
    digest = hashlib.sha256()
    digest.update(pd.util.hash_pandas_object(events, index=False).values.tobytes())
    digest.update(pd.util.hash_pandas_object(coverage, index=False).values.tobytes())
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def build_meta_events_v2(
    symbol: str, timeframe: str
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    symbol, timeframe = symbol.upper(), timeframe.upper()
    required = (event_path(), event_manifest_path(), calendar_manifest_path())
    for path in required:
        if not path.exists():
            raise FileNotFoundError(path)
    v1_manifest = json.loads(event_manifest_path().read_text(encoding="utf-8"))
    if v1_manifest.get("versions", {}).get("meta_feature") != 1:
        raise ValueError("Frozen v1 source must carry meta_feature version 1")
    if file_sha256(event_path()) != v1_manifest.get("parquet_sha256"):
        raise ValueError("Frozen v1 source hash does not match its manifest")

    frame = pd.read_parquet(event_path())
    frame = frame[(frame["symbol"] == symbol) & (frame["timeframe"] == timeframe)].copy()
    frame = frame.sort_values(["signal_ts", "side", "event_id"], kind="stable").reset_index(
        drop=True
    )
    if frame.empty:
        raise ValueError(f"No frozen v1 events for {symbol} {timeframe}")
    if frame["event_id"].duplicated().any():
        raise ValueError("Frozen v1 contains duplicate event IDs")

    currencies = settings.calendar_symbol_currencies.get(symbol)
    if not currencies:
        raise ValueError(f"No calendar currency scope configured for {symbol}")
    calendar_features = build_calendar_feature_frame(frame["signal_ts"], currencies=currencies)
    calendar_manifest_sha = file_sha256(calendar_manifest_path())
    calendar_sha = _calendar_training_source_sha256(frame["signal_ts"])
    for name in ("calendar_coverage_ok", *CALENDAR_MODEL_FEATURES):
        frame[name] = calendar_features[name].to_numpy()
    frame["calendar_source_manifest_sha256"] = calendar_sha
    rejected = int((~frame["calendar_coverage_ok"].fillna(False)).sum())
    frame = frame[frame["calendar_coverage_ok"]].copy().reset_index(drop=True)
    frame["meta_feature_version"] = META_FEATURE_VERSION_V2

    counts = {
        "by_year": frame.assign(year=pd.to_datetime(frame["signal_ts"]).dt.year)
        .groupby("year")
        .size()
        .astype(int)
        .to_dict(),
        "by_side": {str(key): int(value) for key, value in frame.groupby("side").size().items()},
        "by_setup": frame.groupby("primary_setup_id").size().astype(int).to_dict(),
    }
    manifest = {
        "manifest_version": META_EVENT_MANIFEST_VERSION_V2,
        "dataset_contract": f"{symbol}-{timeframe}-meta-events-v2",
        "rows": len(frame),
        "schema_sha256": _schema_sha256(frame),
        "columns": list(frame.columns),
        "model_feature_columns": list(META_MODEL_FEATURES_V2),
        "auxiliary_columns": [name for name in frame.columns if name not in META_MODEL_FEATURES_V2],
        "versions": {
            "bar_feature": settings.bar_feature_version,
            "meta_feature": META_FEATURE_VERSION_V2,
            "meta_label": settings.meta_label_version,
        },
        "source": {
            "v1_path": str(event_path().relative_to(settings.data_dir)),
            "v1_parquet_sha256": file_sha256(event_path()),
            "v1_manifest_sha256": file_sha256(event_manifest_path()),
            "calendar_manifest_path": str(calendar_manifest_path().relative_to(settings.data_dir)),
            "calendar_manifest_sha256": calendar_sha,
            "operational_calendar_manifest_sha256_at_export": calendar_manifest_sha,
            "calendar_scope": list(currencies),
            "historical_schedule_as_of_available": False,
            "historical_schedule_caveat": (
                "Historical pages are retrospective source pages; true publication-time "
                "schedule snapshots are unavailable."
            ),
        },
        "calendar_policy": {
            "impact": "high",
            "future_window_clock_hours": 24,
            "distance_cap_minutes": 7 * 24 * 60,
            "pre_post_window_minutes": 120,
            "coverage": "seven days before signal through 24 hours after signal",
            "uncovered_event_policy": "exclude",
        },
        "label_policy": v1_manifest.get("label_policy"),
        "counts": counts,
        "drops": {"calendar_coverage_unreliable": rejected},
        "forbidden_calendar_inputs": [
            "actual",
            "forecast",
            "previous",
            "revision",
            "release_values_available_at_utc",
        ],
    }
    report = {
        "report_version": 2,
        "status": "accepted" if rejected == 0 else "accepted_with_exclusions",
        "symbol": symbol,
        "timeframe": timeframe,
        "rows": len(frame),
        "source_v1_rows": len(calendar_features),
        "calendar_coverage_rejected": rejected,
        "calendar_scope": list(currencies),
        "calendar_manifest_sha256": calendar_sha,
        "meta_feature_version": META_FEATURE_VERSION_V2,
        "meta_label_version": settings.meta_label_version,
        "outcome_columns_used_to_build_calendar_features": [],
        "training_performed": False,
    }
    return frame, manifest, report


def export_meta_events_v2(symbol: str, timeframe: str) -> int:
    frame, manifest, report = build_meta_events_v2(symbol, timeframe)
    path = event_path_v2()
    _atomic_parquet(path, frame)
    persisted = pd.read_parquet(path)
    manifest = {
        **manifest,
        "schema_sha256": _schema_sha256(persisted),
        "parquet_sha256": file_sha256(path),
    }
    _atomic_json(event_manifest_path_v2(), manifest)
    _atomic_json(event_report_path_v2(symbol.upper(), timeframe.upper()), report)
    return len(persisted)
