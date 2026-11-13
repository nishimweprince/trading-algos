from datetime import date

from gotobi.jpcal import classify_days, gotobi_days, is_jp_business_day


def test_bank_closure_and_holidays():
    assert not is_jp_business_day(date(2025, 1, 2))
    assert not is_jp_business_day(date(2024, 12, 31))
    assert not is_jp_business_day(date(2025, 5, 5))  # Children's Day
    assert is_jp_business_day(date(2025, 5, 2))


def test_rollback_to_previous_business_day():
    g = gotobi_days(date(2024, 12, 1), date(2025, 5, 31))
    assert date(2024, 12, 30) in g  # 5 Jan 2025 (Sun) rolls past the bank closure
    assert date(2025, 5, 2) in g  # 5 May 2025 (holiday) -> Fri 2 May
    assert date(2025, 5, 5) not in g


def test_no_feb_30_and_weekday_filter():
    days = classify_days(date(2025, 2, 1), date(2025, 2, 28))
    gotobi = sorted(d for d, v in days.items() if v)
    # 5 Wed, 10 Mon (excluded), 14 Fri (15 Sat), 20 Thu, 25 Tue; no 30th
    assert gotobi == [date(2025, 2, 5), date(2025, 2, 14), date(2025, 2, 20), date(2025, 2, 25)]
    assert all(1 <= d.weekday() <= 4 for d in days)
