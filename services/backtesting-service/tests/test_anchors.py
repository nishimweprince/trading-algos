from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtesting_service.anchors import (
    bars_in_orb_window,
    build_anchors,
    drift_minutes,
    entry_time,
    opening_range,
    parse_anchor,
    parse_anchor_token,
    session_anchor_ts,
)
from backtesting_service.models import Candle


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_parse_anchor_token() -> None:
    anchor = parse_anchor_token("new_york:America/New_York:08:00")
    assert anchor.name == "new_york"
    assert str(anchor.tz) == "America/New_York"
    assert (anchor.at.hour, anchor.at.minute) == (8, 0)


def test_session_anchor_follows_dst() -> None:
    ny = parse_anchor("new_york", "America/New_York:08:00")
    winter = session_anchor_ts(ny, datetime(2026, 1, 14, 17, 0, tzinfo=UTC))
    summer = session_anchor_ts(ny, datetime(2026, 7, 15, 17, 0, tzinfo=UTC))
    assert winter.astimezone(UTC) == datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    assert summer.astimezone(UTC) == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_entry_time_is_not_before_orb_close() -> None:
    anchor = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    assert entry_time(anchor_ts=anchor, orb_minutes=60, entry_delay_minutes=15) == (
        datetime(2026, 1, 14, 14, 0, tzinfo=UTC)
    )
    assert entry_time(anchor_ts=anchor, orb_minutes=15, entry_delay_minutes=15) == (
        datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    )


def test_opening_range_uses_window_not_one_bar() -> None:
    bars = [
        _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2005, low=1999, c=2002),
        _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2002, h=2010, low=2000, c=2004),
        _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC), o=2004, h=2006, low=2001, c=2003),
        _bar(datetime(2026, 1, 14, 14, 0, tzinfo=UTC), o=2003, h=2004, low=1995, c=1998),
    ]
    anchor = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    window = bars_in_orb_window(bars, timeframe_minutes=15, anchor_ts=anchor, orb_minutes=60)
    assert opening_range(window) == pytest.approx(15.0)


def test_drift_minutes() -> None:
    anchor = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    first = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    assert drift_minutes(first, anchor) == pytest.approx(60.0)


def test_build_anchors_defaults_to_cash_opens() -> None:
    anchors = build_anchors(["tokyo", "london", "new_york"])
    assert [a.name for a in anchors] == ["tokyo", "london", "new_york"]
    assert all(a.at.hour == 8 or a.at.hour == 9 for a in anchors)
