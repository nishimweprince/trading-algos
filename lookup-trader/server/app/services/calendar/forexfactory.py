"""Forex Factory economic calendar parser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
from pathlib import Path

PARSER_VERSION = "1.0.0"
USER_AGENT = "lookup-trader-research/1.0 (+fixture-tests)"


@dataclass(frozen=True)
class CalendarEvent:
    time_utc: datetime
    currency: str
    impact: str
    title: str
    event_date: date


class _CalendarParser(HTMLParser):
    """Minimal row parser for FF calendar table markup."""

    def __init__(self, event_date: date) -> None:
        super().__init__()
        self.event_date = event_date
        self.events: list[CalendarEvent] = []
        self._in_row = False
        self._cells: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "tr" and "calendar__row" in (attr.get("class") or ""):
            self._in_row = True
            self._cells = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._in_row:
            self._in_row = False
            self._flush_row()
            self._cells = []

    def handle_data(self, data: str) -> None:
        if self._in_row:
            text = data.strip()
            if text:
                self._cells.append(text)

    def _flush_row(self) -> None:
        if len(self._cells) < 4:
            return
        time_text, currency, impact, title = self._cells[:4]
        impact_norm = impact.lower()
        if impact_norm not in {"high", "medium", "low"}:
            return
        event_time = _parse_time(time_text)
        if event_time is None:
            return
        dt = datetime.combine(self.event_date, event_time, tzinfo=timezone.utc)
        self.events.append(
            CalendarEvent(
                time_utc=dt,
                currency=currency.upper(),
                impact=impact_norm,
                title=title,
                event_date=self.event_date,
            )
        )


def _parse_time(value: str) -> time | None:
    value = value.strip().lower()
    if not value or value in {"all day", "tentative", "day"}:
        return None
    match = re.match(r"(\d{1,2}):(\d{2})(am|pm)?", value.replace(" ", ""))
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return time(hour=hour, minute=minute)


def parse_day_html(html: str, event_date: date) -> list[CalendarEvent]:
    parser = _CalendarParser(event_date)
    parser.feed(html)
    return parser.events


def day_url(event_date: date) -> str:
    return f"https://www.forexfactory.com/calendar?day={event_date.strftime('%b%d.%Y').lower()}"


def fetch_day_html(event_date: date, cache_dir: Path | None = None) -> str:
    """Fetch day page HTML, caching to disk when cache_dir is provided."""
    cache_dir = cache_dir or Path("data/calendar/raw")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{event_date.isoformat()}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    try:
        import urllib.request

        req = urllib.request.Request(day_url(event_date), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — graceful degradation to manual flag
        raise RuntimeError(f"Forex Factory fetch failed for {event_date}: {exc}") from exc

    cache_path.write_text(html, encoding="utf-8")
    return html
