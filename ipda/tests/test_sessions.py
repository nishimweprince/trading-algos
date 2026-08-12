from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ipda.sessions import active_session, build_windows, parse_window

TOKYO = "Asia/Tokyo:09:00-18:00"
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
        "Asia/Tokyo",  # no window
        "Asia/Tokyo:0900-1800",  # no HH:MM
        "Asia/Tokyo:18:00-09:00",  # end before start
        "Not/AZone:09:00-18:00",  # unknown tz
    ],
)
def test_parse_window_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_window("tokyo", spec)


def test_new_york_window_follows_dst() -> None:
    """The whole point of exchange-local windows: 13:00 UTC is inside the NY cash
    session in winter (EST, 08:00 local) and outside it in summer (EDT, 09:00 local
    is inside, but 12:30 UTC = 08:30 EDT is inside while 12:30 UTC in winter is 07:30
    EST and outside)."""
    windows = build_windows(["new_york"], {"new_york": NEW_YORK})

    # Winter (EST, UTC-5): 08:00 local == 13:00 UTC.
    assert active_session(_utc(2026, 1, 14, 13, 0), windows) == "new_york"
    assert active_session(_utc(2026, 1, 14, 12, 30), windows) is None

    # Summer (EDT, UTC-4): 08:00 local == 12:00 UTC. A fixed-UTC window would
    # have missed this hour entirely.
    assert active_session(_utc(2026, 7, 15, 12, 30), windows) == "new_york"
    assert active_session(_utc(2026, 7, 15, 11, 30), windows) is None


def test_tokyo_window() -> None:
    windows = build_windows(["tokyo"], {"tokyo": TOKYO})

    # Japan does not observe DST: 09:00 JST is 00:00 UTC year-round.
    assert active_session(_utc(2026, 3, 11, 0, 30), windows) == "tokyo"
    assert active_session(_utc(2026, 3, 11, 9, 30), windows) is None


def test_boundaries_are_half_open() -> None:
    windows = build_windows(["tokyo"], {"tokyo": TOKYO})

    assert active_session(_utc(2026, 3, 11, 0, 0), windows) == "tokyo"  # 09:00 JST
    assert active_session(_utc(2026, 3, 11, 9, 0), windows) is None  # 18:00 JST


def test_weekend_is_excluded() -> None:
    windows = build_windows(["new_york"], {"new_york": NEW_YORK})

    # Saturday 2026-01-17, 14:00 UTC = 09:00 EST — inside the hours, wrong day.
    assert active_session(_utc(2026, 1, 17, 14, 0), windows) is None
    assert active_session(_utc(2026, 1, 16, 14, 0), windows) == "new_york"  # Friday


def test_first_matching_window_wins() -> None:
    windows = build_windows(["tokyo", "new_york"], {"tokyo": TOKYO, "new_york": NEW_YORK})

    assert active_session(_utc(2026, 1, 14, 2, 0), windows) == "tokyo"
    assert active_session(_utc(2026, 1, 14, 15, 0), windows) == "new_york"
    assert active_session(_utc(2026, 1, 14, 11, 0), windows) is None  # between sessions


def test_no_windows_means_always_active() -> None:
    assert active_session(_utc(2026, 1, 17, 3, 0), []) == "always"


def test_build_windows_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown trading session 'london'"):
        build_windows(["london"], {})


def test_build_windows_uses_defaults_when_spec_absent() -> None:
    windows = build_windows(["tokyo", "new_york"], {})

    assert [w.name for w in windows] == ["tokyo", "new_york"]
