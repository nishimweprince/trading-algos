from __future__ import annotations

from datetime import date
from pathlib import Path

from app.services.calendar.forexfactory import parse_day_html
from app.services.calendar.store import calendar_flags, register_events_view, upsert_day
import duckdb


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "forexfactory_jul17_2024.html"


def test_parse_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    events = parse_day_html(html, date(2024, 7, 17))
    assert len(events) == 3
    assert events[0].currency == "USD"
    assert events[0].impact == "high"
    assert "Retail Sales" in events[0].title


def test_calendar_flags_within_window(tmp_path, monkeypatch):
    html = FIXTURE.read_text(encoding="utf-8")
    events = parse_day_html(html, date(2024, 7, 17))
    monkeypatch.setattr(
        "app.services.calendar.store.events_parquet_path",
        lambda: tmp_path / "events.parquet",
    )
    upsert_day(events)

    con = duckdb.connect(":memory:")
    register_events_view(con)
    flags = calendar_flags(con, "XAUUSD", events[0].time_utc)
    assert flags["high_impact_today"] is True
    assert len(flags["events"]) >= 1
