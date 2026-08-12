"""Trading-session windows expressed in exchange-local time.

Windows are defined in the exchange's own timezone (``Asia/Tokyo``,
``America/New_York``) and compared after converting the current UTC instant into
that zone, so daylight saving is handled by the tz database rather than by hour
arithmetic. A window that would drift by an hour twice a year is a window that
silently trades the wrong session for weeks.

Windows never wrap past midnight: both defaults are daytime cash sessions. A
spec whose end is not after its start is rejected at load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = frozenset({0, 1, 2, 3, 4})

DEFAULT_SESSION_SPECS: dict[str, str] = {
    "tokyo": "Asia/Tokyo:09:00-18:00",
    "new_york": "America/New_York:08:00-17:00",
}


@dataclass(frozen=True, slots=True)
class SessionWindow:
    name: str
    tz: ZoneInfo
    start: time
    end: time
    weekdays: frozenset[int] = WEEKDAYS

    def contains(self, moment: datetime) -> bool:
        """True when ``moment`` (any tz-aware instant) falls inside this window."""
        local = moment.astimezone(self.tz)
        if local.weekday() not in self.weekdays:
            return False
        return self.start <= local.timetz().replace(tzinfo=None) < self.end


def _parse_time(text: str) -> time:
    hour_text, _, minute_text = text.partition(":")
    if not minute_text:
        raise ValueError(f"session time must be HH:MM, got {text!r}")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"session time out of range: {text!r}")
    return time(hour=hour, minute=minute)


def parse_window(name: str, spec: str) -> SessionWindow:
    """Parse ``TZ_NAME:HH:MM-HH:MM`` (e.g. ``Asia/Tokyo:09:00-18:00``)."""
    zone_text, _, window_text = spec.strip().partition(":")
    if not zone_text or not window_text:
        raise ValueError(f"session {name!r} must be 'TZ:HH:MM-HH:MM', got {spec!r}")
    start_text, _, end_text = window_text.partition("-")
    if not start_text or not end_text:
        raise ValueError(f"session {name!r} must be 'TZ:HH:MM-HH:MM', got {spec!r}")

    try:
        tz = ZoneInfo(zone_text)
    except ZoneInfoNotFoundError as exc:  # missing tzdata on Windows lands here
        raise ValueError(f"unknown timezone {zone_text!r} for session {name!r}") from exc

    start, end = _parse_time(start_text), _parse_time(end_text)
    if end <= start:
        raise ValueError(f"session {name!r} end must be after start, got {spec!r}")
    return SessionWindow(name=name, tz=tz, start=start, end=end)


def build_windows(names: list[str], specs: dict[str, str]) -> list[SessionWindow]:
    """Resolve session names against their specs, defaulting to the built-ins."""
    windows: list[SessionWindow] = []
    for name in names:
        spec = specs.get(name) or DEFAULT_SESSION_SPECS.get(name)
        if spec is None:
            known = ", ".join(sorted(DEFAULT_SESSION_SPECS))
            raise ValueError(f"unknown trading session {name!r}; known sessions: {known}")
        windows.append(parse_window(name, spec))
    return windows


def active_session(moment: datetime, windows: list[SessionWindow]) -> str | None:
    """Name of the first window containing ``moment``.

    An empty window list means "no session restriction", and reports the sentinel
    name ``always`` so callers can log why a signal was allowed through.
    """
    if not windows:
        return "always"
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    for window in windows:
        if window.contains(aware):
            return window.name
    return None
