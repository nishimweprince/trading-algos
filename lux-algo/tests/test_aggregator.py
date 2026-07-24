from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lux_algo.candles import Aggregator, Candle


def _m(minute: int, o: float, h: float, low: float, c: float, v: float = 1.0) -> Candle:
    return Candle(
        start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=minute),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def test_three_minute_bucketing_with_forming_bar() -> None:
    # Minutes 0..4 -> bucket [0,1,2] closed, bucket [3,4] forming (only 2 of 3 minutes).
    minutes = [
        _m(0, 10, 11, 9, 10.5),
        _m(1, 10.5, 12, 10, 11),
        _m(2, 11, 11.5, 10.5, 10.8),
        _m(3, 10.8, 13, 10.7, 12),
        _m(4, 12, 12.5, 11.5, 11.9),
    ]
    series = Aggregator(3).build(minutes)

    assert len(series.closed) == 1
    closed = series.closed[0]
    assert closed.closed is True
    assert closed.start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert closed.open == 10  # first minute's open
    assert closed.high == 12  # max over the three minutes
    assert closed.low == 9
    assert closed.close == 10.8  # last minute's close
    assert closed.volume == 3.0

    forming = series.forming
    assert forming is not None
    assert forming.closed is False
    assert forming.start == datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    assert forming.open == 10.8
    assert forming.high == 13
    assert forming.low == 10.7
    assert forming.close == 11.9  # running close = latest minute (mid-candle)
    assert forming.volume == 2.0


def test_dedup_keeps_latest_minute_data() -> None:
    # Same minute delivered twice with updated high/close (a still-forming 1M candle).
    minutes = [_m(0, 10, 11, 9, 10.5), _m(0, 10, 12, 9, 11.0)]
    series = Aggregator(3).build(minutes)
    assert series.forming is not None
    assert series.forming.high == 12
    assert series.forming.close == 11.0


def test_midnight_aligned_buckets_fall_on_tf_boundaries() -> None:
    # Bars starting at 01:00 -> 3-min buckets begin at :00, :03, :06 of the hour.
    minutes = [
        Candle(
            start=datetime(2026, 1, 1, 1, 0, tzinfo=UTC) + timedelta(minutes=i),
            open=10,
            high=11,
            low=9,
            close=10,
            volume=1.0,
        )
        for i in range(9)
    ]
    series = Aggregator(3).build(minutes)
    minutes_of_hour = sorted(c.start.minute for c in series.closed)
    assert minutes_of_hour == [0, 3]  # closed buckets at 01:00 and 01:03; 01:06 forming


def test_bucket_offset_changes_alignment() -> None:
    minutes = [
        Candle(
            start=datetime(2026, 1, 1, 1, 0, tzinfo=UTC) + timedelta(minutes=i),
            open=10,
            high=11,
            low=9,
            close=10,
            volume=1.0,
        )
        for i in range(9)
    ]
    no_offset = {c.start for c in Aggregator(3).build(minutes).closed}
    with_offset = {c.start for c in Aggregator(3, offset_minutes=1).build(minutes).closed}
    assert no_offset != with_offset
