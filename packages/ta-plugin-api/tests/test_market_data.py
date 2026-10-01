from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ta_contracts import Candle, Timeframe

from ta_plugin_api.testing import assert_closed_utc_candles

NOW = datetime(2026, 3, 2, 10, 7, tzinfo=UTC)


def _bar(ts: datetime) -> Candle:
    return Candle(
        ts=ts, open=1, high=1, low=1, close=1, volume=1, provider="x", source_instrument="X"
    )


def test_accepts_closed_aligned_ascending_bars() -> None:
    bars = [
        _bar(datetime(2026, 3, 2, 10, 0, tzinfo=UTC) - timedelta(minutes=5 * i)) for i in (2, 1, 0)
    ]

    assert_closed_utc_candles(bars, Timeframe.M5, now=NOW)


@pytest.mark.parametrize(
    ("stamps", "message"),
    [
        ([datetime(2026, 3, 2, 10, 10, tzinfo=UTC)], "forming"),
        ([datetime(2026, 3, 2, 10, 3, tzinfo=UTC)], "interval end"),
        (
            [datetime(2026, 3, 2, 10, 0, tzinfo=UTC), datetime(2026, 3, 2, 9, 55, tzinfo=UTC)],
            "ascending",
        ),
    ],
)
def test_rejects_bars_a_consumer_would_misread(stamps: list[datetime], message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        assert_closed_utc_candles([_bar(ts) for ts in stamps], Timeframe.M5, now=NOW)
