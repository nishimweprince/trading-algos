from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from urllib.parse import quote

import duckdb
import pandas as pd
import pytest

from app.config import settings
from app.services.calendar import forexfactory
from app.services.calendar.forexfactory import SOURCE_TIMEZONE, CalendarParseError
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


def test_fetch_pins_the_source_timezone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rendered timezone must not depend on where the fetch runs.

    Forex Factory geolocates the viewer when no preference is set, so an
    unpinned fetch returns America/Chicago from a US-central address and
    America/Los_Angeles from an Azure host. That zone decides which local day an
    event lands on, while features.py and store.py bucket against the
    SOURCE_TIMEZONE constant — so an unpinned fetch silently disagrees with
    every week already ingested.
    """
    captured: dict[str, str] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return FIXTURE.read_bytes()

    def fake_urlopen(request: object, timeout: int | None = None) -> _Response:
        captured.update(request.headers)  # type: ignore[attr-defined]
        return _Response()

    monkeypatch.setattr(forexfactory.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(forexfactory.time_module, "sleep", lambda *_: None)

    forexfactory.fetch_week_html(date(2024, 7, 15), cache_dir=tmp_path / "raw")

    # urllib capitalizes header keys on the Request object.
    assert captured.get("Cookie") == f"fftimezone={quote(SOURCE_TIMEZONE, safe='')}"
    assert SOURCE_TIMEZONE == "America/Chicago"
