"""Causal calendar-feature preview without mutating the frozen event export."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

from app.config import settings
from app.db.duck import register_candles_view
from app.services.calendar.forexfactory import SOURCE_TIMEZONE
from app.services.calendar.store import coverage_parquet_path, events_parquet_path
from app.services.meta_events import event_path

PRE_POST_WINDOW_MINUTES = 120
DISTANCE_CAP_MINUTES = 7 * 24 * 60
HORIZON_BARS = 24

META_PREVIEW_COLUMNS = ("event_id", "signal_ts")
CALENDAR_CAUSAL_COLUMNS = (
    "time_utc",
    "event_date",
    "time_kind",
    "currency",
    "impact",
    "title",
)
SCOPES: dict[str, tuple[str, ...] | None] = {
    "usd": ("USD",),
    "usd_eur_cny": ("USD", "EUR", "CNY"),
    "all": None,
}


def preview_report_path(symbol: str, timeframe: str, date_from: date, date_to: date) -> Path:
    return (
        settings.data_dir
        / "reports"
        / (
            f"calendar-feature-preview-{symbol}-{timeframe}-{date_from.isoformat()}-"
            f"{date_to.isoformat()}-v1.json"
        )
    )


def _covered_dates(frame: pd.DataFrame) -> set[date]:
    reliable = frame[frame["coverage_ok"].fillna(False)]
    return set(pd.to_datetime(reliable["calendar_date"]).dt.date)


def _dates_between(start: date, end: date) -> set[date]:
    return {start + timedelta(days=value) for value in range((end - start).days + 1)}


def _summary(values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if value is not None and not pd.isna(value)]
    if not present:
        return {"count": 0, "missing": len(values)}
    if all(isinstance(value, (bool, np.bool_)) for value in present):
        return {
            "count": len(present),
            "missing": len(values) - len(present),
            "true": int(sum(bool(value) for value in present)),
            "false": int(sum(not bool(value) for value in present)),
        }
    numeric = np.asarray(present, dtype=float)
    return {
        "count": len(present),
        "missing": len(values) - len(present),
        "min": float(numeric.min()),
        "median": float(np.median(numeric)),
        "mean": float(numeric.mean()),
        "max": float(numeric.max()),
        "zero_rate": float((numeric == 0).mean()),
    }


def _features_for_signal(
    signal_ts: pd.Timestamp,
    horizon_end: pd.Timestamp | None,
    high_times: pd.DatetimeIndex,
    covered: set[date],
) -> dict[str, Any]:
    timezone = ZoneInfo(SOURCE_TIMEZONE)
    if high_times.tz is None:
        high_times = high_times.tz_localize("UTC")
    else:
        high_times = high_times.tz_convert("UTC")
    context_start = signal_ts - timedelta(minutes=DISTANCE_CAP_MINUTES)
    context_end = signal_ts + timedelta(minutes=DISTANCE_CAP_MINUTES)
    if horizon_end is not None:
        context_end = max(context_end, horizon_end)
    local_start = context_start.to_pydatetime().astimezone(timezone).date()
    local_end = context_end.to_pydatetime().astimezone(timezone).date()
    reliable = _dates_between(local_start, local_end).issubset(covered) and horizon_end is not None
    if not reliable:
        return {
            "calendar_coverage_ok": False,
            "high_impact_in_horizon": None,
            "mins_to_next_high_impact": None,
            "mins_since_last_high_impact": None,
            "in_pre_news_window": None,
            "in_post_news_window": None,
            "high_impact_count_today": None,
        }

    timestamps_ns = high_times.asi8
    signal_ns = signal_ts.value
    previous_index = int(np.searchsorted(timestamps_ns, signal_ns, side="right")) - 1
    next_index = int(np.searchsorted(timestamps_ns, signal_ns, side="left"))
    since = (
        min(
            DISTANCE_CAP_MINUTES,
            (signal_ns - timestamps_ns[previous_index]) / (60 * 1_000_000_000),
        )
        if previous_index >= 0
        else DISTANCE_CAP_MINUTES
    )
    until = (
        min(
            DISTANCE_CAP_MINUTES,
            (timestamps_ns[next_index] - signal_ns) / (60 * 1_000_000_000),
        )
        if next_index < len(timestamps_ns)
        else DISTANCE_CAP_MINUTES
    )
    utc_day_start = signal_ts.floor("D")
    utc_day_end = utc_day_start + timedelta(days=1)
    horizon_start_index = int(np.searchsorted(timestamps_ns, signal_ns, side="right"))
    horizon_end_index = int(np.searchsorted(timestamps_ns, horizon_end.value, side="right"))
    day_start_index = int(np.searchsorted(timestamps_ns, utc_day_start.value, side="left"))
    day_end_index = int(np.searchsorted(timestamps_ns, utc_day_end.value, side="left"))
    return {
        "calendar_coverage_ok": True,
        "high_impact_in_horizon": horizon_end_index - horizon_start_index,
        "mins_to_next_high_impact": float(until),
        "mins_since_last_high_impact": float(since),
        "in_pre_news_window": bool(until <= PRE_POST_WINDOW_MINUTES),
        "in_post_news_window": bool(since <= PRE_POST_WINDOW_MINUTES),
        "high_impact_count_today": day_end_index - day_start_index,
    }


def build_feature_preview(
    symbol: str,
    timeframe: str,
    date_from: date,
    date_to: date,
    *,
    write_report: bool = True,
) -> dict[str, Any]:
    symbol, timeframe = symbol.upper(), timeframe.upper()
    calendar_path = events_parquet_path()
    coverage_path = coverage_parquet_path()
    meta_path = event_path()
    for required in (calendar_path, coverage_path, meta_path):
        if not required.exists():
            raise FileNotFoundError(required)

    # Explicit column projections are the causal boundary for this pilot.
    meta = pd.read_parquet(meta_path, columns=list(META_PREVIEW_COLUMNS))
    calendar = pd.read_parquet(calendar_path, columns=list(CALENDAR_CAUSAL_COLUMNS))
    coverage = pd.read_parquet(coverage_path)
    meta["signal_ts"] = pd.to_datetime(meta["signal_ts"], utc=True)
    calendar["time_utc"] = pd.to_datetime(calendar["time_utc"], utc=True)
    start = pd.Timestamp(date_from, tz="UTC")
    end = pd.Timestamp(date_to + timedelta(days=1), tz="UTC")
    signals = meta[(meta["signal_ts"] >= start) & (meta["signal_ts"] < end)].copy()

    con = duckdb.connect(":memory:")
    try:
        register_candles_view(con, force=True)
        candles = con.execute(
            "SELECT ts FROM candles WHERE symbol = ? AND timeframe = ? ORDER BY ts",
            [symbol, timeframe],
        ).fetchdf()
    finally:
        con.close()
    candle_times = pd.DatetimeIndex(pd.to_datetime(candles["ts"], utc=True))
    candle_ordinals = {timestamp: index for index, timestamp in enumerate(candle_times)}
    horizon_by_signal: dict[pd.Timestamp, pd.Timestamp | None] = {}
    for signal_ts in signals["signal_ts"]:
        ordinal = candle_ordinals.get(signal_ts)
        horizon_by_signal[signal_ts] = (
            candle_times[ordinal + HORIZON_BARS]
            if ordinal is not None and ordinal + HORIZON_BARS < len(candle_times)
            else None
        )

    covered = _covered_dates(coverage)
    timed_high = calendar[
        (calendar["time_kind"] == "timed")
        & (calendar["impact"] == "high")
        & calendar["time_utc"].notna()
    ]
    scope_reports: dict[str, Any] = {}
    for scope, currencies in SCOPES.items():
        selected = timed_high
        if currencies is not None:
            selected = selected[selected["currency"].isin(currencies)]
        high_times = pd.DatetimeIndex(selected["time_utc"].sort_values())
        rows = [
            _features_for_signal(
                signal_ts,
                horizon_by_signal.get(signal_ts),
                high_times,
                covered,
            )
            for signal_ts in signals["signal_ts"]
        ]
        scope_reports[scope] = {
            "currencies": list(currencies) if currencies is not None else "all",
            "timed_high_impact_events": len(selected),
            "reliable_signals": sum(row["calendar_coverage_ok"] for row in rows),
            "feature_distribution": {
                name: _summary([row[name] for row in rows])
                for name in (
                    "calendar_coverage_ok",
                    "high_impact_in_horizon",
                    "mins_to_next_high_impact",
                    "mins_since_last_high_impact",
                    "in_pre_news_window",
                    "in_post_news_window",
                    "high_impact_count_today",
                )
            },
        }

    report = {
        "report_version": 1,
        "status": "preview_only",
        "symbol": symbol,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "signals": len(signals),
        "meta_source_columns": list(META_PREVIEW_COLUMNS),
        "calendar_source_columns": list(CALENDAR_CAUSAL_COLUMNS),
        "outcome_columns_read": [],
        "horizon_bars": HORIZON_BARS,
        "distance_cap_minutes": DISTANCE_CAP_MINUTES,
        "pre_post_window_minutes": PRE_POST_WINDOW_MINUTES,
        "scopes": scope_reports,
        "meta_feature_version_modified": False,
        "meta_event_export_modified": False,
        "training_performed": False,
    }
    if write_report:
        path = preview_report_path(symbol, timeframe, date_from, date_to)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return report
