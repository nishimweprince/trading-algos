"""Candle model and 1-minute -> target-timeframe aggregation.

The aggregator is deterministic and stateless per poll: given the full list of
1-minute candles returned by the data endpoint, it rebuilds every target-timeframe
bucket from scratch. The most recent bucket that has not yet been superseded by a
later 1-minute candle is treated as the *forming* bar, exactly how Pine Script treats
the realtime bar. This is what lets a signal fire mid-candle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(slots=True)
class Candle:
    """OHLCV candle. ``start`` is the tz-aware bucket/interval start."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True


@dataclass(slots=True)
class AggregatedSeries:
    """Closed target-timeframe candles plus the current forming (partial) candle."""

    closed: list[Candle]
    forming: Candle | None

    def as_series(self) -> list[Candle]:
        """Full evaluation series: closed history followed by the forming bar."""
        return self.closed + ([self.forming] if self.forming is not None else [])


def _bucket_start(ts: datetime, tf_minutes: int, offset_minutes: int) -> datetime:
    """Align ``ts`` down to the start of its target-timeframe bucket.

    Buckets are aligned to UTC midnight (not the raw epoch), so 3-minute bars fall on
    :00/:03/:06 ... of the hour the way chart intraday bars do. ``offset_minutes`` shifts
    that grid when the feed's session boundary is not midnight-aligned.
    """
    ts_utc = ts.astimezone(UTC)
    midnight = ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since = int((ts_utc - midnight).total_seconds() // 60) - offset_minutes
    bucket_min = (minutes_since // tf_minutes) * tf_minutes + offset_minutes
    return midnight + timedelta(minutes=bucket_min)


class Aggregator:
    """Aggregates 1-minute candles into ``tf_minutes`` buckets.

    ``offset_minutes`` shifts bucket boundaries away from the raw UTC epoch grid when
    the data feed's session does not start on an epoch-aligned boundary.
    """

    def __init__(self, tf_minutes: int, offset_minutes: int = 0) -> None:
        if tf_minutes < 1:
            raise ValueError("tf_minutes must be >= 1")
        self.tf_minutes = tf_minutes
        self.offset_minutes = offset_minutes % tf_minutes

    def build(self, minutes: list[Candle]) -> AggregatedSeries:
        if not minutes:
            return AggregatedSeries(closed=[], forming=None)

        # Sort ascending and dedup by minute start, keeping the latest data per minute.
        latest_by_start: dict[datetime, Candle] = {}
        for candle in sorted(minutes, key=lambda c: c.start):
            latest_by_start[candle.start] = candle
        ordered = list(latest_by_start.values())

        buckets: dict[datetime, Candle] = {}
        bucket_order: list[datetime] = []
        for m in ordered:
            start = _bucket_start(m.start, self.tf_minutes, self.offset_minutes)
            existing = buckets.get(start)
            if existing is None:
                buckets[start] = Candle(
                    start=start,
                    open=m.open,
                    high=m.high,
                    low=m.low,
                    close=m.close,
                    volume=m.volume,
                    closed=False,
                )
                bucket_order.append(start)
            else:
                existing.high = max(existing.high, m.high)
                existing.low = min(existing.low, m.low)
                existing.close = m.close  # ordered ascending, so this is the latest minute
                existing.volume += m.volume

        # The last bucket is forming; every earlier bucket is closed.
        forming_start = bucket_order[-1]
        closed: list[Candle] = []
        for start in bucket_order[:-1]:
            candle = buckets[start]
            candle.closed = True
            closed.append(candle)
        forming = buckets[forming_start]
        return AggregatedSeries(closed=closed, forming=forming)

    def bucket_end(self, start: datetime) -> datetime:
        return start + timedelta(minutes=self.tf_minutes)
