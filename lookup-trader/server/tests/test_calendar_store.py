from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.config import settings
from app.services.calendar.forexfactory import CalendarParseError
from app.services.calendar.store import ingest_range, list_events

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "forexfactory_jul17_2024.html"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _two_day_page() -> str:
    html = FIXTURE.read_text(encoding="utf-8")
    return html.replace(
        "]}],\ntime:",
        ']},{"date":"Thu <span>Jul 18</span>","dateline":1721278800,"events":[]}],\ntime:',
    )


def test_range_ingest_is_deterministic_and_preserves_empty_covered_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    cache = tmp_path / "calendar" / "raw"
    cache.mkdir(parents=True)
    (cache / "week_2024-07-15.html").write_text(_two_day_page(), encoding="utf-8")

    report = ingest_range(
        date(2024, 7, 17), date(2024, 7, 18), use_network=False, write_report=True
    )
    assert report["events"] == 4
    assert report["covered_days"] == 2
    events_path = tmp_path / "calendar" / "events.parquet"
    coverage_path = tmp_path / "calendar" / "coverage.parquet"
    before = (_sha256(events_path), _sha256(coverage_path))

    repeat = ingest_range(
        date(2024, 7, 17), date(2024, 7, 18), use_network=False, write_report=True
    )
    assert repeat["events"] == 4
    assert before == (_sha256(events_path), _sha256(coverage_path))
    coverage = pd.read_parquet(coverage_path)
    assert len(coverage) == 2
    con = duckdb.connect(":memory:")
    assert list_events(con, date(2024, 7, 18)) == []


def test_parse_failure_does_not_replace_existing_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    cache = tmp_path / "calendar" / "raw"
    cache.mkdir(parents=True)
    source = cache / "week_2024-07-15.html"
    source.write_text(_two_day_page(), encoding="utf-8")
    ingest_range(date(2024, 7, 17), date(2024, 7, 18), use_network=False)
    events_path = tmp_path / "calendar" / "events.parquet"
    coverage_path = tmp_path / "calendar" / "coverage.parquet"
    before = (_sha256(events_path), _sha256(coverage_path))

    source.write_text("malformed", encoding="utf-8")
    with pytest.raises(CalendarParseError):
        ingest_range(date(2024, 7, 17), date(2024, 7, 18), use_network=False)
    assert before == (_sha256(events_path), _sha256(coverage_path))
