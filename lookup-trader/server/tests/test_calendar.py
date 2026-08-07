from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import calendar as calendar_router
from app.services.calendar.forexfactory import (
    CalendarParseError,
    parse_day_html,
    parse_week_html,
)
from app.services.calendar.store import (
    CalendarCoverageError,
    calendar_flags,
    list_events,
    register_events_view,
    upsert_day,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "forexfactory_jul17_2024.html"


def _page(day_timestamp: int, event_date: str, events: list[dict]) -> str:
    days = [{"date": event_date, "dateline": day_timestamp, "events": events}]
    rendered = "".join(
        f'<tr data-event-id="{event["id"]}" class="calendar__row">'
        f'<td><span class="icon {event["impactClass"]}"></span></td></tr>'
        for event in events
    )
    return (
        "<script>window.meta = {'User Timezone': 'America/Chicago'};"
        "window.calendarComponentStates[1] = {days: "
        f"{json.dumps(days)}, time: 'now'}};</script><table>{rendered}</table>"
    )


def _paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.calendar.store.events_parquet_path",
        lambda: tmp_path / "events.parquet",
    )
    monkeypatch.setattr(
        "app.services.calendar.store.coverage_parquet_path",
        lambda: tmp_path / "coverage.parquet",
    )


def test_parse_structured_fixture_and_masked_event() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_week_html(html, date(2024, 7, 15))
    assert len(parsed.events) == 4
    assert parsed.days == (date(2024, 7, 17),)
    assert parsed.events[0].currency == "USD"
    assert parsed.events[0].impact == "high"
    assert parsed.events[0].time_utc == datetime(2024, 7, 17, 13, 30, tzinfo=UTC)
    assert parsed.events[-1].time_kind == "day_marker"
    assert parsed.events[-1].time_utc is None
    assert len(parse_day_html(html, date(2024, 7, 17))) == 4


def test_parser_rejects_timezone_and_rendered_impact_mismatches() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    with pytest.raises(CalendarParseError, match="timezone"):
        parse_week_html(html.replace("America/Chicago", "UTC"), date(2024, 7, 15))
    with pytest.raises(CalendarParseError, match="rendered impact"):
        parse_week_html(
            html.replace("icon icon--ff-impact-red", "icon icon--ff-impact-ora", 1),
            date(2024, 7, 15),
        )


def test_parser_rejects_bad_utc_cross_check() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    with pytest.raises(CalendarParseError, match="UTC/local-time"):
        parse_week_html(html.replace("1721223000", "1721226600"), date(2024, 7, 15))


def test_timezone_cross_check_handles_winter_and_dst_transition() -> None:
    winter = {
        "id": 1,
        "ebaseId": 1,
        "name": "Winter release",
        "dateline": 1705501800,
        "currency": "USD",
        "impactName": "high",
        "impactClass": "icon--ff-impact-red",
        "timeLabel": "8:30am",
        "timeMasked": False,
        "date": "Jan 17, 2024",
    }
    parsed = parse_week_html(_page(1705471200, "Wed Jan 17", [winter]), date(2024, 1, 15))
    assert parsed.events[0].time_utc == datetime(2024, 1, 17, 14, 30, tzinfo=UTC)

    before = {
        **winter,
        "id": 2,
        "name": "Before DST",
        "dateline": 1710055800,
        "timeLabel": "1:30am",
        "date": "Mar 10, 2024",
    }
    after = {
        **winter,
        "id": 3,
        "name": "After DST",
        "dateline": 1710059400,
        "timeLabel": "3:30am",
        "date": "Mar 10, 2024",
    }
    parsed = parse_week_html(_page(1710050400, "Sun Mar 10", [before, after]), date(2024, 3, 4))
    assert [event.time_utc.hour for event in parsed.events] == [7, 8]

    fall_back = {
        **winter,
        "id": 4,
        "name": "Daylight Saving Time Shift",
        "dateline": 1257058800,
        "timeLabel": "1:00am",
        "date": "Nov 1, 2009",
    }
    parsed = parse_week_html(_page(1257051600, "Sun Nov 1", [fall_back]), date(2009, 10, 26))
    assert parsed.events[0].time_utc == datetime(2009, 11, 1, 7, 0, tzinfo=UTC)


def test_calendar_flags_and_covered_empty_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths(tmp_path, monkeypatch)
    html = FIXTURE.read_text(encoding="utf-8")
    events = parse_day_html(html, date(2024, 7, 17))
    upsert_day(events)

    con = duckdb.connect(":memory:")
    flags = calendar_flags(con, "XAUUSD", events[0].time_utc)
    assert flags["coverage_ok"] is True
    assert flags["high_impact_today"] is True
    assert flags["high_impact_nearby"] is True
    assert flags["events"][0]["title"] == "Retail Sales m/m"
    listed = list_events(con, date(2024, 7, 17))
    assert len(listed) == 4
    assert listed[-1]["time_utc"] is None


def test_uncovered_date_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _paths(tmp_path, monkeypatch)
    con = duckdb.connect(":memory:")
    with pytest.raises(CalendarCoverageError):
        list_events(con, date(2024, 7, 18))

    def db_override():
        connection = duckdb.connect(":memory:")
        register_events_view(connection)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[calendar_router.get_db] = db_override
    try:
        response = TestClient(app).get("/calendar/events", params={"date": "2024-07-18"})
    finally:
        app.dependency_overrides.pop(calendar_router.get_db, None)
    assert response.status_code == 503
