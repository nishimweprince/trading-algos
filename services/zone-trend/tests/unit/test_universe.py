from __future__ import annotations

from datetime import date

from conftest import DAY, HOUR, klines, put

from zone_trend.data.universe import is_usdt_perp, universe_at

ASOF = date(2024, 6, 1)


def daily(paths, symbol, start, days, volume, price=100.0):
    put(paths, "klines", symbol, "1d", klines(start, days, DAY, price=price, volume=volume))


def hourly(paths, symbol, start, hours, spread=0.1, price=100.0):
    df = klines(start, hours, HOUR, price=price)
    df["high"] = df[["open", "close"]].max(axis=1) + spread
    df["low"] = df[["open", "close"]].min(axis=1) - spread
    put(paths, "klines", symbol, "1h", df)


def test_usdt_perp_filter():
    assert is_usdt_perp("BTCUSDT")
    assert not is_usdt_perp("BTCUSDT_240628")
    assert not is_usdt_perp("BTCUSDC")
    assert not is_usdt_perp("USDCUSDT")


def test_ranking_respects_listing_age_delisting_and_quarantine(paths):
    daily(paths, "AAAUSDT", "2023-01-01", 517, volume=1000)  # oldest, mid volume
    daily(paths, "BBBUSDT", "2023-01-01", 517, volume=5000)  # top volume
    daily(paths, "NEWUSDT", "2024-03-01", 92, volume=9000)  # listed < 180 days
    daily(paths, "DEADUSDT", "2023-01-01", 400, volume=9000)  # stopped trading
    daily(paths, "QQQUSDT", "2023-01-01", 517, volume=8000)  # quarantined last week
    daily(paths, "USDCUSDT", "2023-01-01", 517, volume=99999)  # stablecoin
    for s in ("AAAUSDT", "BBBUSDT", "QQQUSDT"):
        hourly(paths, s, "2024-05-01", 31 * 24)

    res = universe_at(
        paths, ASOF, size=10, quarantine={"QQQUSDT": {"2024-05-28": ["gap:klines/1m"]}}
    )
    assert res.symbols == ["BBBUSDT", "AAAUSDT"]
    assert res.missing_1h == []


def test_range_and_tick_exclusions_and_cut(paths):
    for i, s in enumerate(("AUSDT", "BUSDT", "CUSDT", "WILDUSDT", "TICKUSDT")):
        daily(paths, s, "2023-01-01", 517, volume=10_000 - i * 1000)
        hourly(paths, s, "2024-05-01", 31 * 24, spread=5.0 if s == "WILDUSDT" else 0.1)
    filters = {"TICKUSDT": {"tick": 0.1}, "AUSDT": {"tick": 0.001}}  # 0.1 / ~100 = 0.1%
    res = universe_at(paths, ASOF, size=2, filters=filters)
    outcome = {row["symbol"]: row["outcome"] for row in res.ranked}
    assert res.symbols == ["AUSDT", "BUSDT"]
    assert outcome["WILDUSDT"] == "excluded:range"
    assert outcome["TICKUSDT"] == "excluded:tick"
    assert outcome["CUSDT"] == "below_cut"
    assert res.no_tick == ["BUSDT"]


def test_only_bars_closed_before_asof_are_used(paths):
    # A volume spike on the asof day itself must not change the ranking.
    a = klines("2023-01-01", 600, DAY, volume=1000)
    b = klines("2023-01-01", 600, DAY, volume=900)
    b.loc[b["open_dt"].dt.date >= ASOF, ["volume", "quote_volume"]] = 10**9
    put(paths, "klines", "AUSDT", "1d", a)
    put(paths, "klines", "BUSDT", "1d", b)
    res = universe_at(paths, ASOF, size=1)
    assert res.symbols == ["AUSDT"]
