"""1m months aggregate to exact 15m months (the 15m prototype's data input)."""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import MINUTE, klines, put

from zone_trend.data.resample import QUARTER_MS, resample_15m, resample_symbol
from zone_trend.data.store import load

MIN = 60_000


@pytest.fixture
def put_1m_june(paths):
    put(paths, "klines", "TESTUSDT", "1m", klines("2024-06-01", 30 * 1440, MINUTE))
    return paths


def one_minute(n: int, price: float = 100.0) -> pd.DataFrame:
    t0 = int(pd.Timestamp("2024-06-01", tz="UTC").value // 1_000_000)
    t = t0 + pd.Series(range(n), dtype="int64") * MIN
    return pd.DataFrame(
        {
            "open_time": t,
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": 2.0,
            "close_time": t + MIN - 1,
            "quote_volume": 200.0,
            "count": 10,
            "taker_buy_volume": 1.0,
            "taker_buy_quote_volume": 100.0,
        }
    )


def test_resample_15m_aggregates_exact_quarters():
    out = resample_15m(one_minute(30))
    assert len(out) == 2
    first = out.iloc[0]
    assert first["open_time"] == one_minute(1)["open_time"][0]
    assert first["close_time"] == first["open_time"] + QUARTER_MS - 1
    ohlc = (first["open"], first["high"], first["low"], first["close"])
    assert ohlc == (100.0, 101.0, 99.0, 100.5)
    assert (first["volume"], first["quote_volume"], first["count"]) == (30.0, 3000.0, 150)
    assert (first["taker_buy_volume"], first["taker_buy_quote_volume"]) == (15.0, 1500.0)
    assert out["open_time"].diff().dropna().eq(QUARTER_MS).all()


def test_resample_15m_drops_an_incomplete_trailing_quarter():
    out = resample_15m(one_minute(20))
    assert len(out) == 1
    assert resample_15m(one_minute(14)).empty


def test_resample_symbol_writes_15m_months_and_skips_them_next_time(paths, put_1m_june):
    built, skipped = resample_symbol(paths, "TESTUSDT")
    assert (built, skipped) == (1, 0)
    bars = load(paths, "klines", "TESTUSDT", "15m")
    assert len(bars) == 30 * 96  # every quarter of June 2024 is complete
    assert bars["open_time"].min() // QUARTER_MS * QUARTER_MS == bars["open_time"].min()
    assert bars["close_time"].sub(bars["open_time"]).eq(QUARTER_MS - 1).all()
    assert resample_symbol(paths, "TESTUSDT") == (0, 1)
    assert resample_symbol(paths, "TESTUSDT", force=True) == (1, 0)
