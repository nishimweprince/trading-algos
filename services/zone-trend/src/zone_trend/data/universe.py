"""Point-in-time trading universe, rebalanced on the first day of each month.

Eligible at ``asof``: a USDT-quoted perpetual listed at least ``min_listing_days``,
still trading (a daily bar in the two days before ``asof``), and with no
quarantined day in the 30 days before ``asof``. Eligible symbols are ranked by
their 30-day median daily quote volume, using only bars that closed before
``asof``. Walking down that ranking, a symbol is excluded when its median 1H
range is over 3x the pool median, or when tickSize / price exceeds 0.05%; the
first ``size`` survivors are the universe.

With ``coins_only`` (the default), symbols whose exchangeInfo ``underlyingType``
is not COIN are dropped: Binance lists tokenised equities and commodities
(TSLAUSDT, XAUUSDT, ...) as ``TRADIFI_PERPETUAL`` USDT contracts, and they are
not the crypto majors the strategy targets. Delisted symbols have no
exchangeInfo row and are kept (every delisted USDT perpetual so far was a coin).

Delisted symbols stay in the candidate set (their history comes from the Vision
listing), so historical universes carry no survivorship bias. tickSize is only
known from the current exchangeInfo snapshot; for a delisted symbol the tick
check is skipped and that is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from zone_trend.data.store import DataPaths, load

__all__ = ["UniverseResult", "month_starts", "universe_at"]

_DAY_MS = 86_400_000
STABLE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDE", "PYUSD", "EUR"}
LOOKBACK_DAYS = 30
POOL_MULT = 3
RANGE_MULT = 3.0
MAX_TICK_FRAC = 0.0005


@dataclass
class UniverseResult:
    asof: str
    symbols: list[str]
    ranked: list[dict] = field(default_factory=list)  # every candidate looked at, with outcome
    missing_1h: list[str] = field(default_factory=list)
    no_tick: list[str] = field(default_factory=list)


def is_usdt_perp(symbol: str) -> bool:
    if "_" in symbol or not symbol.endswith("USDT"):
        return False
    return symbol.removesuffix("USDT") not in STABLE_BASES


def _ms(d: date) -> int:
    return int(pd.Timestamp(d, tz="UTC").timestamp() * 1000)


def month_starts(start: str, end: str) -> list[date]:
    return [p.to_timestamp().date() for p in pd.period_range(start, end, freq="M")]


class DailyCache:
    """1d klines for every symbol, loaded once for a whole universe history."""

    def __init__(self, paths: DataPaths):
        self.paths = paths
        self.frames = {
            s: load(paths, "klines", s, "1d", columns=["open_time", "close", "quote_volume"])
            for s in paths.symbols("klines", "1d")
            if is_usdt_perp(s)
        }


def _quarantined(days: dict[str, list[str]] | None, start_ms: int, end_ms: int) -> bool:
    if not days:
        return False
    lo = pd.Timestamp(start_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")
    hi = pd.Timestamp(end_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")
    return any(lo <= d < hi for d in days)


def universe_at(
    paths: DataPaths,
    asof: date,
    *,
    size: int = 10,
    min_listing_days: int = 180,
    exclude: tuple[str, ...] = (),
    quarantine: dict[str, dict[str, list[str]]] | None = None,
    filters: dict[str, dict] | None = None,
    daily: DailyCache | None = None,
    coins_only: bool = True,
) -> UniverseResult:
    daily = daily or DailyCache(paths)
    quarantine = quarantine or {}
    filters = filters or {}
    t = _ms(asof)
    window_start = t - LOOKBACK_DAYS * _DAY_MS

    eligible: list[tuple[str, float, float]] = []  # (symbol, median quote volume, last close)
    for symbol, df in daily.frames.items():
        if symbol in exclude or df.empty:
            continue
        underlying = filters.get(symbol, {}).get("underlying_type")
        if coins_only and underlying not in (None, "COIN"):
            continue
        past = df[df["open_time"] + _DAY_MS <= t]  # bars closed before asof
        if past.empty or past["open_time"].iloc[0] > t - min_listing_days * _DAY_MS:
            continue
        if past["open_time"].iloc[-1] < t - 2 * _DAY_MS:
            continue  # delisted or halted
        if _quarantined(quarantine.get(symbol), window_start, t):
            continue
        recent = past[past["open_time"] >= window_start]
        eligible.append(
            (symbol, float(recent["quote_volume"].median()), float(past["close"].iloc[-1]))
        )

    eligible.sort(key=lambda row: row[1], reverse=True)
    pool = eligible[: size * POOL_MULT]
    result = UniverseResult(asof.isoformat(), [])

    ranges: dict[str, float] = {}
    for symbol, _, _ in pool:
        h1 = load(
            paths,
            "klines",
            symbol,
            "1h",
            window_start,
            t,
            columns=["open_time", "high", "low", "close"],
        )
        if h1.empty:
            result.missing_1h.append(symbol)
            continue
        ranges[symbol] = float(((h1["high"] - h1["low"]) / h1["close"]).median())
    pool_median = float(np.median(list(ranges.values()))) if ranges else float("nan")

    for symbol, volume, close in pool:
        row = {"symbol": symbol, "median_quote_volume": volume, "range_1h": ranges.get(symbol)}
        if symbol in ranges and ranges[symbol] > RANGE_MULT * pool_median:
            row["outcome"] = "excluded:range"
        elif (tick := filters.get(symbol, {}).get("tick")) and tick / close > MAX_TICK_FRAC:
            row["outcome"] = "excluded:tick"
        elif len(result.symbols) >= size:
            row["outcome"] = "below_cut"
        else:
            row["outcome"] = "selected"
            result.symbols.append(symbol)
            if not filters.get(symbol, {}).get("tick"):
                result.no_tick.append(symbol)
        result.ranked.append(row)
    return result
