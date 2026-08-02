from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    """Normalize naive (assume UTC) or aware datetimes to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_utc_iso(dt: datetime) -> str:
    """Serialize a datetime as UTC ISO-8601 with Z suffix."""
    return to_utc(dt).isoformat().replace("+00:00", "Z")
