"""Japanese business days and Gotobi trade days, following the paper's rules.

* Gotobi dates are the 5th, 10th, 15th, 20th, 25th and 30th of each month
  (the paper's definition; February has no 30th, and the 31st is not one).
* If a Gotobi date is not a Japanese business day, the previous business day
  takes its place (the paper: "the previous business days were included if
  Gotobi days were holiday").
* Only Tuesday to Friday are used, for Gotobi and control days alike, because
  the 03:00 JST entry on a Monday falls on Sunday afternoon in New York, when
  the market is closed.

Japanese business days exclude weekends, national holidays (``holidays.JP``)
and the bank closure of 31 Dec - 3 Jan.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import holidays

GOTOBI_DOMS = (5, 10, 15, 20, 25, 30)


@lru_cache(maxsize=64)
def _jp_holidays(year: int) -> holidays.HolidayBase:
    return holidays.JP(years=year)


def is_jp_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _jp_holidays(d.year):
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    return True


def previous_business_day(d: date) -> date:
    while not is_jp_business_day(d):
        d -= timedelta(days=1)
    return d


def gotobi_days(start: date, end: date) -> set[date]:
    """Effective Gotobi business days in [start, end], before the weekday filter."""
    out: set[date] = set()
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        for dom in GOTOBI_DOMS:
            try:
                d = date(y, m, dom)
            except ValueError:
                continue  # 30 Feb
            eff = previous_business_day(d)
            if start <= eff <= end:
                out.add(eff)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def tradable(d: date) -> bool:
    """Tuesday-Friday Japanese business day."""
    return 1 <= d.weekday() <= 4 and is_jp_business_day(d)


def classify_days(start: date, end: date) -> dict[date, bool]:
    """Every tradable day in [start, end] -> True if Gotobi, False if control."""
    g = gotobi_days(start, end)
    out: dict[date, bool] = {}
    d = start
    while d <= end:
        if tradable(d):
            out[d] = d in g
        d += timedelta(days=1)
    return out
