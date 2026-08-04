from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    """Normalize naive (assume UTC) or aware datetimes to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_utc_series(series):
    """Normalize a timestamp column to UTC.

    Candles are stored as TIMESTAMP WITH TIME ZONE, and pandas renders those in
    the machine's local zone — so `.hour` on a bar read straight out of DuckDB is
    a local hour. Session bands and day-of-week are defined in UTC, so every
    timestamp has to pass through here before anything reads a field off it.
    """
    import pandas as pd

    return pd.to_datetime(series, utc=True)


def to_utc_iso(dt: datetime) -> str:
    """Serialize a datetime as UTC ISO-8601 with Z suffix."""
    return to_utc(dt).isoformat().replace("+00:00", "Z")
