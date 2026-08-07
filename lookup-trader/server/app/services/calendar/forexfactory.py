"""Forex Factory weekly economic-calendar parser and cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time as time_module
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

PARSER_VERSION = "2.0.0"
USER_AGENT = "lookup-trader-research/2.0 (+cached-fixture-validation)"
SOURCE_TIMEZONE = "America/Chicago"
FETCH_DELAY_SECONDS = 3.0

_IMPACT_BY_CLASS = {
    "icon--ff-impact-red": "high",
    "icon--ff-impact-ora": "medium",
    "icon--ff-impact-yel": "low",
    "icon--ff-impact-gra": "non_economic",
}
_IMPACT_BY_NAME = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "holiday": "non_economic",
    "non-economic": "non_economic",
    "non_economic": "non_economic",
}


class CalendarParseError(ValueError):
    """Raised when a source page cannot be accepted as reliable."""


@dataclass(frozen=True)
class CalendarEvent:
    time_utc: datetime | None
    currency: str
    impact: str
    title: str
    event_date: date
    source_event_id: str = ""
    source_definition_id: str | None = None
    source_timezone: str = SOURCE_TIMEZONE
    time_label: str = ""
    time_kind: str = "timed"
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    revision: str | None = None
    release_values_available_at_utc: datetime | None = None
    source_week: date | None = None
    raw_sha256: str | None = None


@dataclass(frozen=True)
class ParsedWeek:
    source_week: date
    source_timezone: str
    days: tuple[date, ...]
    events: tuple[CalendarEvent, ...]
    raw_sha256: str


class _RenderedEventIndex(HTMLParser):
    """Collect rendered event IDs and CSS impact classes for cross-checking."""

    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, str] = {}
        self._event_id: str | None = None
        self._impact: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "tr" and "calendar__row" in classes:
            self._event_id = attributes.get("data-event-id")
            self._impact = None
        if self._event_id:
            for css_class in classes:
                if css_class in _IMPACT_BY_CLASS:
                    self._impact = _IMPACT_BY_CLASS[css_class]

    def handle_endtag(self, tag: str) -> None:
        if tag != "tr" or self._event_id is None:
            return
        if self._impact is None:
            raise CalendarParseError(
                f"Rendered event {self._event_id} has no recognized impact class"
            )
        if self._event_id in self.events:
            raise CalendarParseError(f"Duplicate rendered event ID {self._event_id}")
        self.events[self._event_id] = self._impact
        self._event_id = None
        self._impact = None


def _source_timezone(html: str) -> str:
    match = re.search(r"['\"]User Timezone['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
    if not match:
        raise CalendarParseError("Forex Factory source timezone is missing")
    timezone_name = match.group(1)
    if timezone_name != SOURCE_TIMEZONE:
        raise CalendarParseError(
            f"Unexpected Forex Factory timezone {timezone_name!r}; expected {SOURCE_TIMEZONE!r}"
        )
    return timezone_name


def _structured_days(html: str) -> list[dict]:
    marker = re.search(
        r"window\.calendarComponentStates\[\d+\]\s*=\s*\{\s*days\s*:\s*",
        html,
    )
    if not marker:
        raise CalendarParseError("Structured Forex Factory calendar payload is missing")
    try:
        value, _ = json.JSONDecoder().raw_decode(html[marker.end() :])
    except json.JSONDecodeError as exc:
        raise CalendarParseError(f"Structured calendar payload is invalid: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise CalendarParseError("Structured calendar payload contains no days")
    return value


def _parse_clock(value: str) -> time:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(am|pm)\s*", value.lower())
    if not match:
        raise CalendarParseError(f"Unrecognized unmasked event time {value!r}")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise CalendarParseError(f"Invalid event time {value!r}")
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def _time_kind(label: str, masked: bool) -> str:
    if not masked:
        return "timed"
    normalized = label.strip().lower()
    if normalized == "all day":
        return "all_day"
    if re.fullmatch(r"day\s+\d+", normalized):
        return "day_marker"
    return "masked"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_week_html(html: str, source_week: date) -> ParsedWeek:
    """Parse and validate a complete weekly page.

    The structured payload supplies stable IDs and epoch timestamps. The rendered
    table is independently checked so a source-format drift fails closed.
    """
    timezone_name = _source_timezone(html)
    timezone = ZoneInfo(timezone_name)
    raw_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
    structured_days = _structured_days(html)

    rendered = _RenderedEventIndex()
    rendered.feed(html)

    parsed_days: list[date] = []
    events: list[CalendarEvent] = []
    seen_ids: set[str] = set()
    for day in structured_days:
        try:
            day_timestamp = int(day["dateline"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarParseError("Calendar day has no valid dateline") from exc
        local_day = datetime.fromtimestamp(day_timestamp, UTC).astimezone(timezone).date()
        if local_day in parsed_days:
            raise CalendarParseError(f"Duplicate calendar day {local_day}")
        parsed_days.append(local_day)

        for raw in day.get("events", []):
            source_event_id = str(raw.get("id", "")).strip()
            if not source_event_id:
                raise CalendarParseError(f"Event on {local_day} has no source ID")
            if source_event_id in seen_ids:
                raise CalendarParseError(f"Duplicate source event ID {source_event_id}")
            seen_ids.add(source_event_id)

            try:
                event_date = datetime.strptime(str(raw["date"]), "%b %d, %Y").date()
            except (KeyError, ValueError) as exc:
                raise CalendarParseError(f"Event {source_event_id} has an invalid date") from exc
            if event_date != local_day:
                raise CalendarParseError(
                    f"Event {source_event_id} date {event_date} disagrees with day {local_day}"
                )

            impact_name = _IMPACT_BY_NAME.get(str(raw.get("impactName", "")).lower())
            impact_class = _IMPACT_BY_CLASS.get(str(raw.get("impactClass", "")))
            if impact_name is None or impact_class is None or impact_name != impact_class:
                raise CalendarParseError(
                    f"Event {source_event_id} has inconsistent impact metadata"
                )
            if rendered.events.get(source_event_id) != impact_name:
                raise CalendarParseError(
                    f"Event {source_event_id} disagrees with the rendered impact class"
                )

            time_label = str(raw.get("timeLabel", "")).strip()
            masked = bool(raw.get("timeMasked", False))
            kind = _time_kind(time_label, masked)
            time_utc: datetime | None = None
            if not masked:
                try:
                    time_utc = datetime.fromtimestamp(int(raw["dateline"]), UTC)
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    raise CalendarParseError(
                        f"Event {source_event_id} has an invalid dateline"
                    ) from exc
                expected_local = datetime.combine(
                    event_date, _parse_clock(time_label), tzinfo=timezone
                )
                # During the autumn DST fold, a local clock value such as 01:00
                # occurs twice. The source epoch disambiguates it; accept either
                # valid fold while still rejecting an unrelated timestamp.
                expected_utc = (
                    expected_local.astimezone(UTC),
                    expected_local.replace(fold=1).astimezone(UTC),
                )
                if min(abs((time_utc - value).total_seconds()) for value in expected_utc) > 60:
                    raise CalendarParseError(
                        f"Event {source_event_id} UTC/local-time cross-check failed"
                    )

            title = str(raw.get("name", "")).strip()
            currency = str(raw.get("currency", "")).strip().upper()
            if not title or not currency:
                raise CalendarParseError(f"Event {source_event_id} lacks title or currency")
            events.append(
                CalendarEvent(
                    source_event_id=source_event_id,
                    source_definition_id=_optional_text(raw.get("ebaseId")),
                    time_utc=time_utc,
                    event_date=event_date,
                    source_timezone=timezone_name,
                    time_label=time_label,
                    time_kind=kind,
                    currency=currency,
                    impact=impact_name,
                    title=title,
                    actual=_optional_text(raw.get("actual")),
                    forecast=_optional_text(raw.get("forecast")),
                    previous=_optional_text(raw.get("previous")),
                    revision=_optional_text(raw.get("revision")),
                    release_values_available_at_utc=time_utc,
                    source_week=source_week,
                    raw_sha256=raw_sha256,
                )
            )

    if set(rendered.events) != seen_ids:
        missing = sorted(set(rendered.events).symmetric_difference(seen_ids))[:5]
        raise CalendarParseError(f"Structured/rendered event IDs disagree: {missing}")
    if parsed_days != sorted(parsed_days):
        raise CalendarParseError("Calendar days are not chronological")
    return ParsedWeek(
        source_week=source_week,
        source_timezone=timezone_name,
        days=tuple(parsed_days),
        events=tuple(events),
        raw_sha256=raw_sha256,
    )


def parse_day_html(html: str, event_date: date) -> list[CalendarEvent]:
    """Compatibility wrapper for callers that request one local calendar day."""
    week = parse_week_html(html, event_date)
    return [event for event in week.events if event.event_date == event_date]


def week_url(source_week: date) -> str:
    return f"https://www.forexfactory.com/calendar?week={source_week.strftime('%b%d.%Y').lower()}"


def day_url(event_date: date) -> str:
    return f"https://www.forexfactory.com/calendar?day={event_date.strftime('%b%d.%Y').lower()}"


def fetch_week_html(
    source_week: date,
    cache_dir: Path | None = None,
    *,
    use_network: bool = True,
    force_refresh: bool = False,
) -> str:
    """Read a cached week or fetch a timestamped live snapshot politely."""
    cache_dir = cache_dir or Path("data/calendar/raw")
    cache_path = cache_dir / f"week_{source_week.isoformat()}.html"
    if cache_path.exists() and not force_refresh:
        return cache_path.read_text(encoding="utf-8", errors="replace")
    if not use_network:
        if force_refresh:
            raise ValueError("force_refresh requires network access")
        raise FileNotFoundError(f"No cached Forex Factory page for week {source_week}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(week_url(source_week), headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8", errors="replace")
            retrieved = datetime.now(UTC)
            snapshot_dir = cache_dir / "live" / f"week={source_week.isoformat()}"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = retrieved.strftime("%Y%m%dT%H%M%S%fZ")
            snapshot_path = snapshot_dir / f"retrieved={stamp}.html"
            snapshot_path.write_text(html, encoding="utf-8")
            temporary = cache_path.with_name(f".{cache_path.name}.tmp")
            try:
                temporary.write_text(html, encoding="utf-8")
                os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
            time_module.sleep(FETCH_DELAY_SECONDS)
            return html
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After")
            delay = (
                float(retry_after) if retry_after and retry_after.isdigit() else 3.0 * 3**attempt
            )
            time_module.sleep(max(FETCH_DELAY_SECONDS, delay))
        except (OSError, TimeoutError) as exc:
            last_error = exc
            time_module.sleep(FETCH_DELAY_SECONDS * 3**attempt)
    raise RuntimeError(f"Forex Factory fetch failed for week {source_week}: {last_error}")


def fetch_day_html(event_date: date, cache_dir: Path | None = None) -> str:
    """Backward-compatible day fetch; new ingestion uses weekly pages."""
    cache_dir = cache_dir or Path("data/calendar/raw")
    cache_path = cache_dir / f"{event_date.isoformat()}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    request = urllib.request.Request(day_url(event_date), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html
