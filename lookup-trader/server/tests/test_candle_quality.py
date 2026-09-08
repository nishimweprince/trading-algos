from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.services.calendar.store import closure_dates
from app.services.candle_quality import unexpected_gaps


def _hourly(start: str, end: str) -> pd.DataFrame:
    ts = pd.date_range(start, end, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
    )


def test_maintenance_pause_and_weekend_stay_quiet():
    frame = _hourly("2026-08-05T00:00:00Z", "2026-08-09T23:00:00Z")
    # Drop the whole of Saturday 2026-08-08: a Friday-to-Sunday hole spans
    # the weekend and stays quiet.
    frame = frame[frame["ts"].dt.date != date(2026, 8, 8)]
    assert unexpected_gaps(frame) == []


def test_weekday_hole_longer_than_two_hours_is_unexpected():
    frame = pd.concat(
        [
            _hourly("2026-08-05T20:00:00Z", "2026-08-05T22:00:00Z"),
            _hourly("2026-08-06T01:00:00Z", "2026-08-06T02:00:00Z"),
        ]
    )
    gaps = unexpected_gaps(frame)
    assert len(gaps) == 1
    assert gaps[0]["after"] == "2026-08-05T22:00:00+00:00"
    assert gaps[0]["before"] == "2026-08-06T01:00:00+00:00"


def test_gap_touching_a_known_closure_is_explained():
    frame = pd.concat(
        [
            _hourly("2026-09-07T17:00:00Z", "2026-09-07T19:00:00Z"),
            _hourly("2026-09-07T23:00:00Z", "2026-09-08T01:00:00Z"),
        ]
    )
    assert len(unexpected_gaps(frame)) == 1
    assert unexpected_gaps(frame, known_closures={date(2026, 9, 7)}) == []
    # A closure on an unrelated date changes nothing.
    assert len(unexpected_gaps(frame, known_closures={date(2026, 9, 8)})) == 1


def test_closure_dates_reads_all_day_bank_holidays(tmp_path: Path):
    frame = pd.DataFrame(
        {
            "event_date": [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 7)],
            "currency": ["USD", "USD", "EUR"],
            "time_kind": ["all_day", "timed", "all_day"],
        }
    )
    path = tmp_path / "events.parquet"
    frame.to_parquet(path, index=False)

    assert closure_dates(date(2026, 9, 1), date(2026, 9, 30), calendar_file=path) == {
        date(2026, 9, 7)
    }
    assert (
        closure_dates(date(2026, 9, 8), date(2026, 9, 30), calendar_file=path) == set()
    )


def test_closure_dates_is_best_effort_on_missing_store(tmp_path: Path):
    assert (
        closure_dates(date(2026, 9, 1), date(2026, 9, 30), calendar_file=tmp_path / "nope.parquet")
        == set()
    )
    assert closure_dates(date(2026, 9, 30), date(2026, 9, 1)) == set()
