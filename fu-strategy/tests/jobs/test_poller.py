from datetime import datetime, timezone

from app.jobs.poller import MarketPoller


def test_seconds_until_next_minute_from_mid_minute():
    now = datetime(2026, 4, 30, 15, 2, 34, tzinfo=timezone.utc)

    assert MarketPoller._seconds_until_next_minute(now) == 26.0


def test_seconds_until_next_minute_from_exact_minute():
    now = datetime(2026, 4, 30, 15, 3, 0, tzinfo=timezone.utc)

    assert MarketPoller._seconds_until_next_minute(now) == 60.0
