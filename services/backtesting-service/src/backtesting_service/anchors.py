"""Explicit session anchors, drift detection, and opening-range windows.

Session membership used to be inferred from window edges. At coarse resolution the first
member bar can open hours after the cash open, which silently mislabels the trade. Anchors
are clock times; a signal whose first bar opens more than ``ANCHOR_TOLERANCE_MINUTES`` after
the anchor is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Candle
from .sessions import WEEKDAYS, SessionWindow

DEFAULT_ANCHOR_SPECS: dict[str, str] = {
    "tokyo": "Asia/Tokyo:09:00",
    "london": "Europe/London:08:00",
    "new_york": "America/New_York:08:00",
}


@dataclass(frozen=True, slots=True)
class SessionAnchor:
    name: str
    tz: ZoneInfo
    at: time


def _parse_hhmm(text: str) -> time:
    hour_text, _, minute_text = text.partition(":")
    if not minute_text:
        raise ValueError(f"anchor time must be HH:MM, got {text!r}")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"anchor time out of range: {text!r}")
    return time(hour=hour, minute=minute)


def parse_anchor(name: str, spec: str) -> SessionAnchor:
    """Parse ``TZ:HH:MM`` (e.g. ``America/New_York:08:00``)."""
    zone_text, _, time_text = spec.strip().partition(":")
    if not zone_text or not time_text:
        raise ValueError(f"anchor {name!r} must be 'TZ:HH:MM', got {spec!r}")
    try:
        tz = ZoneInfo(zone_text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {zone_text!r} for anchor {name!r}") from exc
    return SessionAnchor(name=name, tz=tz, at=_parse_hhmm(time_text))


def parse_anchor_token(token: str) -> SessionAnchor:
    """Parse ``name:TZ:HH:MM``."""
    name, _, rest = token.strip().partition(":")
    if not name or not rest:
        raise ValueError(f"SESSION_ANCHORS entry must be 'name:TZ:HH:MM', got {token!r}")
    return parse_anchor(name.lower(), rest)


def anchor_from_window(window: SessionWindow) -> SessionAnchor:
    return SessionAnchor(name=window.name, tz=window.tz, at=window.start)


def build_anchors(names: list[str], specs: dict[str, str] | None = None) -> list[SessionAnchor]:
    """Resolve session names to anchors, defaulting to each session's cash open."""
    merged = dict(DEFAULT_ANCHOR_SPECS)
    if specs:
        merged.update(specs)
    anchors: list[SessionAnchor] = []
    for name in names:
        spec = merged.get(name)
        if spec is None:
            known = ", ".join(sorted(merged))
            raise ValueError(f"unknown session anchor {name!r}; known: {known}")
        anchors.append(parse_anchor(name, spec))
    return anchors


def session_anchor_ts(anchor: SessionAnchor, moment: datetime) -> datetime:
    """Anchor instant on the local calendar date of ``moment``."""
    local = moment.astimezone(anchor.tz)
    return datetime.combine(local.date(), anchor.at, tzinfo=anchor.tz)


def is_anchor_weekday(anchor: SessionAnchor, moment: datetime) -> bool:
    return moment.astimezone(anchor.tz).weekday() in WEEKDAYS


def drift_minutes(bar_open: datetime, anchor_ts: datetime) -> float:
    return (bar_open - anchor_ts).total_seconds() / 60.0


def bar_open_ts(bar: Candle, timeframe_minutes: int) -> datetime:
    return bar.ts - timedelta(minutes=timeframe_minutes)


def bars_in_orb_window(
    bars: list[Candle],
    *,
    timeframe_minutes: int,
    anchor_ts: datetime,
    orb_minutes: int,
) -> list[Candle]:
    """Bars whose open falls in ``[anchor, anchor + ORB_MINUTES)``."""
    orb_end = anchor_ts + timedelta(minutes=orb_minutes)
    selected: list[Candle] = []
    for bar in bars:
        open_ts = bar_open_ts(bar, timeframe_minutes)
        if anchor_ts <= open_ts < orb_end:
            selected.append(bar)
    return selected


def opening_range(bars: list[Candle]) -> float | None:
    if not bars:
        return None
    return max(bar.high for bar in bars) - min(bar.low for bar in bars)


def entry_time(
    *,
    anchor_ts: datetime,
    orb_minutes: int,
    entry_delay_minutes: int,
) -> datetime:
    """Fill time: after the ORB closes, and not before ``anchor + ENTRY_DELAY``."""
    orb_end = anchor_ts + timedelta(minutes=orb_minutes)
    scheduled = anchor_ts + timedelta(minutes=entry_delay_minutes)
    return max(orb_end, scheduled)


def percentile_50(values: list[float]) -> float | None:
    if not values:
        return None
    return float(median(values))


def session_day_key(session: str, anchor_ts: datetime) -> str:
    return f"{session}:{anchor_ts.isoformat()}"
