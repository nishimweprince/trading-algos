from datetime import UTC, datetime

from walkforward_patterns.clock import (
    fmt,
    in_no_entry,
    parse,
    research_to_utc,
    tod,
    trading_day,
    utc_to_research,
    weekday,
)
from walkforward_patterns.exits import SimPosition, TimeExit


def test_research_clock_is_new_york_with_dst():
    # summer: 21:45 UTC = 17:45 EDT; winter: 21:45 UTC = 16:45 EST
    assert fmt(utc_to_research(datetime(2026, 7, 10, 21, 45, tzinfo=UTC))) == "2026-07-10 17:45"
    assert fmt(utc_to_research(datetime(2026, 1, 9, 21, 45, tzinfo=UTC))) == "2026-01-09 16:45"
    assert research_to_utc(parse("2026-07-10 16:45")) == datetime(2026, 7, 10, 20, 45, tzinfo=UTC)
    assert research_to_utc(parse("2026-01-09 16:45")) == datetime(2026, 1, 9, 21, 45, tzinfo=UTC)


def test_trading_day_and_windows():
    t = parse("2026-11-12 17:00")  # Thursday 17:00 starts Friday's trading day
    assert weekday(trading_day(t)) == 4 and tod(t) == 0
    assert weekday(trading_day(t - 1)) == 3
    assert in_no_entry(parse("2026-11-12 16:00")) and in_no_entry(parse("2026-11-12 18:59"))
    assert not in_no_entry(parse("2026-11-12 19:00")) and not in_no_entry(parse("2026-11-12 15:59"))


def test_time_exit_rules():
    j = 100
    te = TimeExit(j)
    assert te.due(j + 2880, parse("2026-11-10 10:00"), None) == "TIME"
    assert te.due(j + 5, parse("2026-11-13 16:44"), parse("2026-11-13 16:43")) is None
    assert te.due(j + 5, parse("2026-11-13 16:45"), parse("2026-11-13 16:44")) == "FRIDAY"
    # data gap over the Friday close: first bar of the next trading day closes it
    assert te.due(j + 5, parse("2026-11-15 18:00"), parse("2026-11-13 16:30")) == "FRIDAY"


def test_sim_position_sl_first_and_gap_fill():
    p = SimPosition("S01", 1, 0, 0, 100.0, 92.0, 116.0, 8.0, 0.3, 0)
    assert p.on_m1(0, parse("2026-11-10 10:00"), None, 100, 117, 91, 100)  # both touched: SL
    assert p.reason == "SL" and p.exit_px == 92.0
    p = SimPosition("S01", -1, 0, 0, 100.0, 108.0, 84.0, 8.0, 0.0, 0)
    assert not p.on_m1(0, parse("2026-11-10 10:00"), None, 100, 101, 99, 100)
    assert p.on_m1(1, parse("2026-11-10 10:01"), parse("2026-11-10 10:00"), 110, 111, 109, 110)
    assert p.exit_px == 110 and p.R == -10 / 8
