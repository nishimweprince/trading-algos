"""The research clock.

Every rule in the walk-forward research is written against the timestamps of the HistData M1 files. The SPEC says
those are "EST fixed at UTC-5", but the files are America/New_York local time WITH daylight saving: gold reopens at
18:00 and FX at 17:00 on the label in every month of the year (a fixed UTC-5 clock would show 17:00 / 16:00 in
summer). So "research time" is New York wall-clock time, and live UTC timestamps are converted with the
America/New_York zone, never with a fixed -5h.

Internally a research timestamp is an integer number of minutes since 1970-01-01 00:00 on that naive wall clock
(exactly how the packed npz files store it).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DAY = 1440
EPOCH = datetime(1970, 1, 1)

NO_ENTRY = (16 * 60, 19 * 60)  # no new entries 16:00-18:59 research time
FRIDAY_FLAT_TOD = 1425  # minutes since the 17:00 trading-day start = 16:45 research time
MAX_ENTRY_DELAY = 30  # minutes from the signal bar close to the entry minute


def tod(t: int) -> int:
    """Minutes since the 17:00 trading-day start."""
    return (t % DAY + 420) % DAY


def clock_minute(t: int) -> int:
    """Minutes since midnight on the research wall clock."""
    return t % DAY


def trading_day(t: int) -> int:
    """Trading day (17:00 -> 17:00) as days since epoch: the date of t + 7h."""
    return (t + 420) // DAY


def weekday(day: int) -> int:
    """Monday=0 .. Sunday=6 for a days-since-epoch value (1970-01-01 was a Thursday)."""
    return (day + 3) % 7


def in_no_entry(t: int) -> bool:
    m = clock_minute(t)
    return NO_ENTRY[0] <= m < NO_ENTRY[1]


def utc_to_research(dt: datetime) -> int:
    """UTC datetime (aware, or naive meaning UTC) -> research minutes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(NY).replace(tzinfo=None)
    return int((local - EPOCH).total_seconds() // 60)


def research_to_utc(t: int) -> datetime:
    """Research minutes -> aware UTC datetime. Ambiguous fall-back minutes resolve to the first occurrence."""
    local = EPOCH + timedelta(minutes=t)
    return local.replace(tzinfo=NY, fold=0).astimezone(UTC)


def fmt(t: int) -> str:
    return (EPOCH + timedelta(minutes=t)).strftime("%Y-%m-%d %H:%M")


def parse(s: str) -> int:
    return int((datetime.fromisoformat(s) - EPOCH).total_seconds() // 60)
