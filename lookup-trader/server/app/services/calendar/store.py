"""Persist and query economic calendar events."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from app.config import settings
from app.services.calendar.forexfactory import PARSER_VERSION, CalendarEvent, fetch_day_html, parse_day_html


def events_parquet_path() -> Path:
    return settings.data_dir / "calendar" / "events.parquet"


def register_events_view(con: duckdb.DuckDBPyConnection) -> None:
    path = events_parquet_path()
    if path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW economic_events AS SELECT * FROM read_parquet('{path}')"
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE VIEW economic_events AS
            SELECT
              CAST(NULL AS TIMESTAMP) AS time_utc,
              CAST(NULL AS VARCHAR) AS currency,
              CAST(NULL AS VARCHAR) AS impact,
              CAST(NULL AS VARCHAR) AS title,
              CAST(NULL AS DATE) AS event_date,
              CAST(NULL AS VARCHAR) AS parser_version
            WHERE 1 = 0
            """
        )


def upsert_day(events: list[CalendarEvent]) -> int:
    if not events:
        return 0
    path = events_parquet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        [
            {
                "time_utc": e.time_utc,
                "currency": e.currency,
                "impact": e.impact,
                "title": e.title,
                "event_date": e.event_date,
                "parser_version": PARSER_VERSION,
            }
            for e in events
        ]
    )
    if path.exists():
        existing = pd.read_parquet(path)
        day = events[0].event_date
        existing = existing[existing["event_date"] != day]
        frame = pd.concat([existing, frame], ignore_index=True)
    frame.to_parquet(path, index=False)
    return len(events)


def ingest_day(event_date: date, *, use_network: bool = True) -> int:
    cache_dir = settings.data_dir / "calendar" / "raw"
    if use_network:
        html = fetch_day_html(event_date, cache_dir=cache_dir)
    else:
        cache_path = cache_dir / f"{event_date.isoformat()}.html"
        if not cache_path.exists():
            raise FileNotFoundError(f"No cached HTML for {event_date}")
        html = cache_path.read_text(encoding="utf-8", errors="replace")
    events = parse_day_html(html, event_date)
    return upsert_day(events)


def list_events(con: duckdb.DuckDBPyConnection, event_date: date) -> list[dict]:
    register_events_view(con)
    rows = con.execute(
        """
        SELECT time_utc, currency, impact, title
        FROM economic_events
        WHERE event_date = ?
        ORDER BY time_utc
        """,
        [event_date],
    ).fetchall()
    return [
        {
            "time_utc": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
            "currency": row[1],
            "impact": row[2],
            "title": row[3],
        }
        for row in rows
    ]


def calendar_flags(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    ts: datetime,
) -> dict:
    register_events_view(con)
    currencies = settings.calendar_symbol_currencies.get(symbol.upper(), ["USD"])
    window = timedelta(hours=settings.calendar_impact_hours)
    day = ts.date()
    rows = con.execute(
        """
        SELECT time_utc, currency, impact, title
        FROM economic_events
        WHERE event_date = ?
          AND impact = 'high'
          AND currency IN (SELECT unnest(?))
        ORDER BY time_utc
        """,
        [day, currencies],
    ).fetchall()

    events = []
    high_impact_today = False
    for time_utc, currency, impact, title in rows:
        if hasattr(time_utc, "to_pydatetime"):
            time_utc = time_utc.to_pydatetime()
        if time_utc.tzinfo is None:
            time_utc = time_utc.replace(tzinfo=ts.tzinfo or datetime.now().astimezone().tzinfo)
        if abs(time_utc - ts) <= window:
            high_impact_today = True
        events.append(
            {
                "time_utc": time_utc.isoformat(),
                "currency": currency,
                "impact": impact,
                "title": title,
            }
        )

    return {"high_impact_today": high_impact_today, "events": events}
