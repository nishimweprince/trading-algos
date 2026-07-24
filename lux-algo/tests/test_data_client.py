from __future__ import annotations

from datetime import UTC, datetime

from lux_algo.data_client import parse_candles


def test_parse_list_of_objects_with_iso_time() -> None:
    payload = [
        {"time": "2026-01-01T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "vol": 10},
        {"time": "2026-01-01T00:01:00Z", "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0},
    ]
    candles = parse_candles(payload)
    assert len(candles) == 2
    assert candles[0].start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert candles[0].close == 1.5
    assert candles[1].volume == 0.0  # missing volume defaults to 0


def test_parse_wrapped_in_data_key_and_epoch_seconds() -> None:
    payload = {"data": [{"t": 1767225600, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]}
    candles = parse_candles(payload)
    assert candles[0].start == datetime.fromtimestamp(1767225600, tz=UTC)


def test_parse_epoch_milliseconds_matches_seconds() -> None:
    row = {"open": 1, "high": 2, "low": 0.5, "close": 1.5}
    ms = parse_candles([{"timestamp": 1767225600000, **row}])
    sec = parse_candles([{"timestamp": 1767225600, **row}])
    assert ms[0].start == sec[0].start == datetime.fromtimestamp(1767225600, tz=UTC)


def test_parse_array_rows() -> None:
    payload = [[1767225600, 1, 2, 0.5, 1.5, 100], [1767225660, 1.5, 2.5, 1.0, 2.0]]
    candles = parse_candles(payload)
    assert len(candles) == 2
    assert candles[0].volume == 100.0
    assert candles[1].volume == 0.0
    assert candles[0].start < candles[1].start
