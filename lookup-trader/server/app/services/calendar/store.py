"""Deterministically persist, audit, and query economic-calendar events."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from app.config import settings
from app.services.calendar.forexfactory import (
    PARSER_VERSION,
    SOURCE_TIMEZONE,
    CalendarEvent,
    ParsedWeek,
    fetch_week_html,
    parse_week_html,
)

CALENDAR_DATASET_VERSION = 1


class CalendarCoverageError(RuntimeError):
    """Raised when a query window is not backed by trusted calendar data."""


def events_parquet_path() -> Path:
    return settings.data_dir / "calendar" / "events.parquet"


def coverage_parquet_path() -> Path:
    return settings.data_dir / "calendar" / "coverage.parquet"


def calendar_manifest_path() -> Path:
    return settings.data_dir / "calendar" / "manifest.json"


def calendar_report_path(
    date_from: date,
    date_to: date,
    *,
    historical_backfill: bool = False,
) -> Path:
    kind = "backfill" if historical_backfill else "pilot"
    return (
        settings.data_dir
        / "reports"
        / (f"calendar-{kind}-{date_from.isoformat()}-{date_to.isoformat()}-v1.json")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _week_starts(date_from: date, date_to: date) -> list[date]:
    current = date_from - timedelta(days=date_from.weekday())
    result: list[date] = []
    while current <= date_to:
        result.append(current)
        current += timedelta(days=7)
    return result


def _date_range(date_from: date, date_to: date) -> list[date]:
    return [date_from + timedelta(days=offset) for offset in range((date_to - date_from).days + 1)]


def _events_frame(events: list[CalendarEvent]) -> pd.DataFrame:
    rows = []
    for event in events:
        source_event_id = (
            event.source_event_id
            or hashlib.sha256(
                (
                    f"{event.event_date}|{event.time_utc}|{event.currency}|"
                    f"{event.impact}|{event.title}"
                ).encode()
            ).hexdigest()[:24]
        )
        rows.append(
            {
                "event_id": f"ff:{source_event_id}",
                "source": "forexfactory",
                "source_event_id": source_event_id,
                "source_definition_id": event.source_definition_id,
                "time_utc": event.time_utc,
                "event_date": event.event_date,
                "source_timezone": event.source_timezone,
                "time_label": event.time_label,
                "time_kind": event.time_kind,
                "currency": event.currency,
                "impact": event.impact,
                "title": event.title,
                "actual": event.actual,
                "forecast": event.forecast,
                "previous": event.previous,
                "revision": event.revision,
                "release_values_available_at_utc": event.release_values_available_at_utc,
                "source_week": event.source_week,
                "parser_version": PARSER_VERSION,
                "raw_sha256": event.raw_sha256,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "event_id",
                "source",
                "source_event_id",
                "source_definition_id",
                "time_utc",
                "event_date",
                "source_timezone",
                "time_label",
                "time_kind",
                "currency",
                "impact",
                "title",
                "actual",
                "forecast",
                "previous",
                "revision",
                "release_values_available_at_utc",
                "source_week",
                "parser_version",
                "raw_sha256",
            ]
        )
    frame["time_utc"] = pd.to_datetime(frame["time_utc"], utc=True)
    frame["release_values_available_at_utc"] = pd.to_datetime(
        frame["release_values_available_at_utc"], utc=True
    )
    return frame.sort_values(
        ["event_date", "time_utc", "source_event_id"],
        na_position="last",
        kind="stable",
        ignore_index=True,
    )


def _coverage_frame(weeks: list[ParsedWeek], date_from: date, date_to: date) -> pd.DataFrame:
    by_day: dict[date, ParsedWeek] = {}
    for week in weeks:
        for calendar_date in week.days:
            if calendar_date in by_day:
                raise ValueError(f"Calendar date {calendar_date} occurs in multiple weekly pages")
            by_day[calendar_date] = week
    missing = [value for value in _date_range(date_from, date_to) if value not in by_day]
    if missing:
        raise CalendarCoverageError(
            f"Calendar source does not cover requested dates: {missing[:5]}"
        )

    rows = []
    for calendar_date in _date_range(date_from, date_to):
        week = by_day[calendar_date]
        day_events = [event for event in week.events if event.event_date == calendar_date]
        rows.append(
            {
                "calendar_date": calendar_date,
                "coverage_ok": True,
                "source_week": week.source_week,
                "source_timezone": week.source_timezone,
                "event_count": len(day_events),
                "timed_event_count": sum(event.time_kind == "timed" for event in day_events),
                "masked_event_count": sum(event.time_kind != "timed" for event in day_events),
                "parser_version": PARSER_VERSION,
                "raw_sha256": week.raw_sha256,
            }
        )
    return pd.DataFrame(rows).sort_values("calendar_date", ignore_index=True)


def _merge_date_range(
    existing: pd.DataFrame,
    replacement: pd.DataFrame,
    date_column: str,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    if existing.empty:
        return replacement.copy()
    existing_dates = pd.to_datetime(existing[date_column]).dt.date
    preserved = existing[(existing_dates < date_from) | (existing_dates > date_to)]
    return pd.concat([preserved, replacement], ignore_index=True)


def _atomic_parquet(frame: pd.DataFrame, path: Path, sort_columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = frame.sort_values(sort_columns, na_position="last", kind="stable", ignore_index=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        ordered.to_parquet(temporary, index=False)
        pd.read_parquet(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ingest_range(
    date_from: date,
    date_to: date,
    *,
    use_network: bool = True,
    write_report: bool = False,
    historical_backfill: bool = False,
    force_refresh: bool = False,
    progress: Callable[[int, int, date, bool], None] | None = None,
) -> dict[str, Any]:
    if date_to < date_from:
        raise ValueError("Calendar end date must not precede start date")
    cache_dir = settings.data_dir / "calendar" / "raw"
    weeks: list[ParsedWeek] = []
    source_files: list[dict[str, Any]] = []
    source_weeks = _week_starts(date_from, date_to)
    for index, source_week in enumerate(source_weeks, start=1):
        source_path = cache_dir / f"week_{source_week.isoformat()}.html"
        cached = source_path.exists()
        html = fetch_week_html(
            source_week,
            cache_dir,
            use_network=use_network,
            force_refresh=force_refresh,
        )
        parsed = parse_week_html(html, source_week)
        weeks.append(parsed)
        source_files.append(
            {
                "source_week": source_week.isoformat(),
                "path": str(source_path.relative_to(settings.data_dir)),
                "bytes": source_path.stat().st_size,
                "sha256": parsed.raw_sha256,
            }
        )
        if progress is not None:
            progress(index, len(source_weeks), source_week, cached)

    selected_events = [
        event for week in weeks for event in week.events if date_from <= event.event_date <= date_to
    ]
    event_frame = _events_frame(selected_events)
    if event_frame["source_event_id"].duplicated().any():
        duplicates = event_frame.loc[
            event_frame["source_event_id"].duplicated(False), "source_event_id"
        ].tolist()
        raise ValueError(f"Duplicate source event IDs: {duplicates[:5]}")
    coverage_frame = _coverage_frame(weeks, date_from, date_to)

    event_path = events_parquet_path()
    coverage_path = coverage_parquet_path()
    existing_events = pd.read_parquet(event_path) if event_path.exists() else pd.DataFrame()
    existing_coverage = pd.read_parquet(coverage_path) if coverage_path.exists() else pd.DataFrame()
    merged_events = _merge_date_range(
        existing_events, event_frame, "event_date", date_from, date_to
    )
    if not merged_events.empty and merged_events["source_event_id"].duplicated().any():
        raise ValueError("A source event ID conflicts with an event outside the requested range")
    merged_coverage = _merge_date_range(
        existing_coverage, coverage_frame, "calendar_date", date_from, date_to
    )

    _atomic_parquet(
        merged_events,
        event_path,
        ["event_date", "time_utc", "source_event_id"],
    )
    _atomic_parquet(merged_coverage, coverage_path, ["calendar_date"])

    impact_counts = Counter(event.impact for event in selected_events)
    currency_counts = Counter(event.currency for event in selected_events)
    time_kind_counts = Counter(event.time_kind for event in selected_events)
    manifest = {
        "calendar_dataset_version": CALENDAR_DATASET_VERSION,
        "parser_version": PARSER_VERSION,
        "source": "forexfactory",
        "source_timezone": SOURCE_TIMEZONE,
        "scope": "historical_backfill" if historical_backfill else "pilot",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "events_path": str(event_path.relative_to(settings.data_dir)),
        "events_sha256": _sha256(event_path),
        "coverage_path": str(coverage_path.relative_to(settings.data_dir)),
        "coverage_sha256": _sha256(coverage_path),
        "event_rows": len(merged_events),
        "coverage_rows": len(merged_coverage),
        "auxiliary_release_columns": ["actual", "forecast", "previous", "revision"],
        "causal_schedule_columns": [
            "time_utc",
            "event_date",
            "time_kind",
            "currency",
            "impact",
            "title",
        ],
        "source_files": source_files,
    }
    _atomic_json(manifest, calendar_manifest_path())

    report = {
        "report_version": 1,
        "status": "accepted" if bool(coverage_frame["coverage_ok"].all()) else "rejected",
        "scope": "historical_backfill" if historical_backfill else "pilot",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "weeks": len(weeks),
        "covered_days": int(coverage_frame["coverage_ok"].sum()),
        "requested_days": len(coverage_frame),
        "events": len(event_frame),
        "unique_source_event_ids": int(event_frame["source_event_id"].nunique()),
        "duplicate_source_event_ids": int(event_frame["source_event_id"].duplicated().sum()),
        "by_time_kind": dict(sorted(time_kind_counts.items())),
        "by_impact": dict(sorted(impact_counts.items())),
        "by_currency": dict(sorted(currency_counts.items())),
        "high_impact_usd": sum(
            event.impact == "high" and event.currency == "USD" for event in selected_events
        ),
        "manifest_sha256": hashlib.sha256(calendar_manifest_path().read_bytes()).hexdigest(),
        "historical_backfill_approved": historical_backfill,
        "historical_backfill_complete": historical_backfill
        and bool(coverage_frame["coverage_ok"].all()),
        "training_feature_source_ready": historical_backfill
        and bool(coverage_frame["coverage_ok"].all()),
        "model_artifacts_modified": False,
    }
    if write_report:
        _atomic_json(
            report,
            calendar_report_path(
                date_from,
                date_to,
                historical_backfill=historical_backfill,
            ),
        )
    return report


def ingest_day(event_date: date, *, use_network: bool = True) -> int:
    report = ingest_range(event_date, event_date, use_network=use_network)
    return int(report["events"])


def upsert_day(events: list[CalendarEvent]) -> int:
    """Compatibility helper for fixture tests and manually supplied source rows."""
    if not events:
        return 0
    day = events[0].event_date
    if any(event.event_date != day for event in events):
        raise ValueError("upsert_day accepts exactly one local calendar date")
    event_frame = _events_frame(events)
    event_path = events_parquet_path()
    existing = pd.read_parquet(event_path) if event_path.exists() else pd.DataFrame()
    merged = _merge_date_range(existing, event_frame, "event_date", day, day)
    _atomic_parquet(merged, event_path, ["event_date", "time_utc", "source_event_id"])
    coverage = pd.DataFrame(
        [
            {
                "calendar_date": day,
                "coverage_ok": True,
                "source_week": events[0].source_week or day,
                "source_timezone": events[0].source_timezone,
                "event_count": len(events),
                "timed_event_count": sum(event.time_kind == "timed" for event in events),
                "masked_event_count": sum(event.time_kind != "timed" for event in events),
                "parser_version": PARSER_VERSION,
                "raw_sha256": events[0].raw_sha256 or "fixture",
            }
        ]
    )
    coverage_path = coverage_parquet_path()
    existing_coverage = pd.read_parquet(coverage_path) if coverage_path.exists() else pd.DataFrame()
    merged_coverage = _merge_date_range(existing_coverage, coverage, "calendar_date", day, day)
    _atomic_parquet(merged_coverage, coverage_path, ["calendar_date"])
    return len(events)


def register_events_view(con: duckdb.DuckDBPyConnection) -> None:
    path = events_parquet_path()
    if path.exists():
        escaped = str(path).replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW economic_events AS SELECT * FROM read_parquet('{escaped}')"
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE VIEW economic_events AS
            SELECT CAST(NULL AS VARCHAR) AS event_id,
                   CAST(NULL AS TIMESTAMPTZ) AS time_utc,
                   CAST(NULL AS DATE) AS event_date,
                   CAST(NULL AS VARCHAR) AS time_kind,
                   CAST(NULL AS VARCHAR) AS currency,
                   CAST(NULL AS VARCHAR) AS impact,
                   CAST(NULL AS VARCHAR) AS title
            WHERE 1 = 0
            """
        )


def register_coverage_view(con: duckdb.DuckDBPyConnection) -> None:
    path = coverage_parquet_path()
    if path.exists():
        escaped = str(path).replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW calendar_coverage AS SELECT * FROM read_parquet('{escaped}')"
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE VIEW calendar_coverage AS
            SELECT CAST(NULL AS DATE) AS calendar_date,
                   CAST(NULL AS BOOLEAN) AS coverage_ok
            WHERE 1 = 0
            """
        )


def _require_coverage(con: duckdb.DuckDBPyConnection, dates: list[date]) -> None:
    register_coverage_view(con)
    rows = con.execute(
        "SELECT calendar_date FROM calendar_coverage "
        "WHERE coverage_ok IS TRUE AND calendar_date IN (SELECT unnest(?))",
        [dates],
    ).fetchall()
    covered = {row[0] for row in rows}
    missing = [value for value in dates if value not in covered]
    if missing:
        raise CalendarCoverageError(f"Calendar coverage is unavailable for {missing[:5]}")


def list_events(con: duckdb.DuckDBPyConnection, event_date: date) -> list[dict[str, Any]]:
    _require_coverage(con, [event_date])
    register_events_view(con)
    rows = con.execute(
        """
        SELECT time_utc, currency, impact, title, time_kind, time_label
        FROM economic_events
        WHERE event_date = ?
        ORDER BY time_utc NULLS LAST, source_event_id
        """,
        [event_date],
    ).fetchall()
    return [
        {
            "time_utc": row[0].isoformat() if row[0] is not None else None,
            "currency": row[1],
            "impact": row[2],
            "title": row[3],
            "time_kind": row[4],
            "time_label": row[5],
        }
        for row in rows
    ]


def calendar_flags(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    ts: datetime,
) -> dict[str, Any]:
    register_events_view(con)
    timezone = ZoneInfo(SOURCE_TIMEZONE)
    window = timedelta(hours=settings.calendar_impact_hours)
    start, end = ts - window, ts + window
    start_day = start.astimezone(timezone).date()
    end_day = end.astimezone(timezone).date()
    _require_coverage(con, _date_range(start_day, end_day))
    currencies = settings.calendar_symbol_currencies.get(symbol.upper(), ["USD"])
    rows = con.execute(
        """
        SELECT time_utc, currency, impact, title
        FROM economic_events
        WHERE time_utc >= ? AND time_utc <= ?
          AND impact = 'high'
          AND currency IN (SELECT unnest(?))
        ORDER BY time_utc
        """,
        [start, end, currencies],
    ).fetchall()
    events = [
        {
            "time_utc": row[0].isoformat(),
            "currency": row[1],
            "impact": row[2],
            "title": row[3],
        }
        for row in rows
    ]
    nearby = bool(events)
    return {
        "coverage_ok": True,
        "high_impact_nearby": nearby,
        "high_impact_today": nearby,
        "events": events,
    }
