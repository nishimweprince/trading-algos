from __future__ import annotations

from conftest import HOUR, MINUTE, klines, put

from zone_trend.data.store import load, read_snapshot, write_snapshot
from zone_trend.data.validate import validate_symbol


def _consistent_pair(start: str, hours: int):
    """1m bars and the 1h bars they resample to."""
    m1 = klines(start, hours * 60, MINUTE)
    hour = m1["open_time"] // HOUR * HOUR
    g = m1.groupby(hour)
    h1 = klines(start, hours, HOUR)
    h1["open"] = g["open"].first().to_numpy()
    h1["high"] = g["high"].max().to_numpy()
    h1["low"] = g["low"].min().to_numpy()
    h1["close"] = g["close"].last().to_numpy()
    h1["volume"] = g["volume"].sum().to_numpy()
    return m1, h1


def test_clean_data_has_no_quarantine(paths):
    m1, h1 = _consistent_pair("2024-01-01", 48)
    put(paths, "klines", "BTCUSDT", "1m", m1)
    put(paths, "klines", "BTCUSDT", "1h", h1)
    put(paths, "markPriceKlines", "BTCUSDT", "1h", h1)
    rep = validate_symbol(paths, "BTCUSDT", tick=0.1)
    assert rep.quarantine == {} and rep.flags == {}
    assert all(s.missing_bars == 0 and s.ohlc_bad == 0 for s in rep.series)


def test_gap_ohlc_and_resample_failures_quarantine_their_days(paths):
    m1, h1 = _consistent_pair("2024-01-01", 72)
    m1 = m1.drop(index=range(100, 110))  # 10-minute gap on day 1
    h1.loc[30, "low"] = h1.loc[30, "high"] + 1  # broken bar on day 2
    h1.loc[60, "volume"] *= 2  # resample mismatch on day 3
    put(paths, "klines", "BTCUSDT", "1m", m1)
    put(paths, "klines", "BTCUSDT", "1h", h1)
    rep = validate_symbol(paths, "BTCUSDT", tick=0.1)
    m1_series = next(s for s in rep.series if s.tf == "1m")
    assert m1_series.missing_bars == 10 and len(m1_series.gaps) == 1
    assert "gap:klines/1m" in rep.quarantine["2024-01-01"]
    assert "ohlc:klines/1h" in rep.quarantine["2024-01-02"]
    assert rep.quarantine["2024-01-03"] == ["resample:volume"]
    # the gap hour is incomplete, so it is not compared at all
    assert rep.resample_volume_bad == 1


def test_mark_divergence_is_flagged_not_quarantined_by_default(paths):
    h1 = klines("2024-01-01", 24, HOUR)
    mark = h1.copy()
    mark.loc[5, ["close", "high"]] *= 1.05
    put(paths, "klines", "BTCUSDT", "1h", h1)
    put(paths, "markPriceKlines", "BTCUSDT", "1h", mark)
    rep = validate_symbol(paths, "BTCUSDT")
    assert rep.flags == {"2024-01-01": ["mark_last"]} and rep.quarantine == {}
    strict = validate_symbol(paths, "BTCUSDT", strict=True)
    assert strict.quarantine == {"2024-01-01": ["mark_last"]}


def test_snapshot_id_tracks_data_and_quarantine(paths):
    put(paths, "klines", "BTCUSDT", "1h", klines("2024-01-01", 24, HOUR))
    files = [(p, 24) for p in paths.parquet.glob("*/*/*/*.parquet")]
    a = write_snapshot(paths, files, {}, {})
    assert write_snapshot(paths, files, {}, {"note": "reports do not count"}) == a
    b = write_snapshot(paths, files, {"BTCUSDT": {"2024-01-01": ["gap"]}}, {})
    assert b != a and read_snapshot(paths)["snapshot_id"] == b
    assert read_snapshot(paths, a)["quarantine"] == {}


def test_month_files_round_trip_and_derive_datetimes(paths):
    df = klines("2024-01-01", 48, HOUR)
    put(paths, "klines", "BTCUSDT", "1h", df)
    out = load(paths, "klines", "BTCUSDT", "1h")
    assert out.drop(columns="open_dt").equals(df.drop(columns="open_dt"))
    assert out["open_dt"].equals(df["open_dt"])


def test_mark_gaps_are_flags_unless_strict(paths):
    h1 = klines("2024-01-01", 48, HOUR)
    put(paths, "klines", "BTCUSDT", "1h", h1)
    put(paths, "markPriceKlines", "BTCUSDT", "1h", h1.drop(index=range(24, 30)))
    rep = validate_symbol(paths, "BTCUSDT")
    assert rep.quarantine == {} and rep.flags == {"2024-01-02": ["gap:markPriceKlines/1h"]}
    assert validate_symbol(paths, "BTCUSDT", strict=True).quarantine == rep.flags
