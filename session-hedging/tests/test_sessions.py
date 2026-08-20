from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sessions import active_session, build_windows, parse_window

TOKYO = "Asia/Tokyo:09:00-18:00"
LONDON = "Europe/London:08:00-16:30"
NEW_YORK = "America/New_York:08:00-17:00"


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_parse_window_reads_zone_and_bounds() -> None:
    window = parse_window("tokyo", TOKYO)

    assert window.name == "tokyo"
    assert str(window.tz) == "Asia/Tokyo"
    assert (window.start.hour, window.end.hour) == (9, 18)


@pytest.mark.parametrize(
    "spec",
    [
        "Asia/Tokyo",
        "Asia/Tokyo:0900-1800",
        "Asia/Tokyo:18:00-09:00",
        "Not/AZone:09:00-18:00",
    ],
)
def test_parse_window_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_window("tokyo", spec)


def test_new_york_window_follows_dst() -> None:
    windows = build_windows(["new_york"], {"new_york": NEW_YORK})

    assert active_session(_utc(2026, 1, 14, 13, 0), windows) == "new_york"
    assert active_session(_utc(2026, 1, 14, 12, 30), windows) is None
    assert active_session(_utc(2026, 7, 15, 12, 30), windows) == "new_york"
    assert active_session(_utc(2026, 7, 15, 11, 30), windows) is None


def test_london_window_follows_dst() -> None:
    windows = build_windows(["london"], {"london": LONDON})

    # Winter GMT: 08:00 local == 08:00 UTC.
    assert active_session(_utc(2026, 1, 14, 8, 0), windows) == "london"
    assert active_session(_utc(2026, 1, 14, 7, 45), windows) is None
    # Summer BST: 08:00 local == 07:00 UTC.
    assert active_session(_utc(2026, 7, 15, 7, 15), windows) == "london"
    assert active_session(_utc(2026, 7, 15, 6, 45), windows) is None


def test_tokyo_window() -> None:
    windows = build_windows(["tokyo"], {"tokyo": TOKYO})

    assert active_session(_utc(2026, 3, 11, 0, 30), windows) == "tokyo"
    assert active_session(_utc(2026, 3, 11, 9, 30), windows) is None


def test_boundaries_are_half_open() -> None:
    windows = build_windows(["tokyo"], {"tokyo": TOKYO})

    assert active_session(_utc(2026, 3, 11, 0, 0), windows) == "tokyo"
    assert active_session(_utc(2026, 3, 11, 9, 0), windows) is None


def test_weekend_is_excluded() -> None:
    windows = build_windows(["new_york"], {"new_york": NEW_YORK})

    assert active_session(_utc(2026, 1, 17, 14, 0), windows) is None
    assert active_session(_utc(2026, 1, 16, 14, 0), windows) == "new_york"


def test_build_windows_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown trading session 'sydney'"):
        build_windows(["sydney"], {})


def test_build_windows_uses_defaults_including_london() -> None:
    windows = build_windows(["tokyo", "london", "new_york"], {})

    assert [w.name for w in windows] == ["tokyo", "london", "new_york"]
