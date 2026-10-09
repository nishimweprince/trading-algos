"""Data validation: the spec's six checks, producing a quarantine list.

1. Monotonic ``open_time`` and no duplicates (duplicates are counted; the store
   already keeps the last copy).
2. Gaps against the expected bar grid; missing ranges are listed, nothing is
   filled.
3. OHLC sanity: ``low <= min(open, close) <= max(open, close) <= high`` and
   ``volume >= 0``.
4. 1m klines resampled to 1h against native 1h: close within one tick, volume
   within 0.1%.
5. Mark against last: hours whose 1h closes differ by more than 2% are flagged.
6. A symbol-day that fails 1-4 on the last-price klines is quarantined; the
   backtester treats it as no-trade.

Flagged, not quarantined, unless ``strict=True`` (the spec's literal reading):

- Mark/last divergence: they legitimately diverge in fast crashes, and dropping
  exactly those days would flatter a backtest.
- Gaps in mark or index klines. Vision is missing whole days of them (BTCUSDT:
  2019-12-25..30, 2021-07-24..27, ...) while last-price klines are complete.
  Index prices feed nothing in the strategy, and the broker simulator must fall
  back to last-price 1m for stop triggers anyway, since positions stay open
  through no-trade days.

On BTC/ETH 2019-09..2026-10 the default leaves about a dozen quarantined days
per symbol (ten each), all 1m volume defects around exchange incidents.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from zone_trend.data.store import DataPaths, load

__all__ = ["SeriesReport", "SymbolReport", "validate_symbol", "INTERVAL_MS"]

INTERVAL_MS = {"1m": 60_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
KLINE_DATASETS = ("klines", "markPriceKlines", "indexPriceKlines")
_COLS = ["open_time", "open", "high", "low", "close", "volume"]
_DAY_MS = 86_400_000
MAX_LISTED_GAPS = 50


def _day(ms: int | np.integer) -> str:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").strftime("%Y-%m-%d")


@dataclass
class SeriesReport:
    dataset: str
    tf: str
    rows: int = 0
    duplicates: int = 0
    non_monotonic: int = 0
    missing_bars: int = 0
    gaps: list[tuple[str, str]] = field(default_factory=list)  # (first missing, last missing)
    ohlc_bad: int = 0


@dataclass
class SymbolReport:
    symbol: str
    series: list[SeriesReport] = field(default_factory=list)
    resample_close_bad: int = 0
    resample_volume_bad: int = 0
    mark_divergent_hours: int = 0
    quarantine: dict[str, list[str]] = field(default_factory=dict)
    flags: dict[str, list[str]] = field(default_factory=dict)


def check_series(df: pd.DataFrame, step_ms: int, rep: SeriesReport, bad: dict) -> pd.DataFrame:
    """Checks 1-3 on one kline series; returns it sorted and de-duplicated.

    Failing days are added to ``bad`` (the caller decides whether that means
    quarantine or a flag).
    """
    label = f"{rep.dataset}/{rep.tf}"
    rep.rows = len(df)
    if df.empty:
        return df
    times = df["open_time"].to_numpy()
    rep.non_monotonic = int((np.diff(times) <= 0).sum())
    rep.duplicates = int(df["open_time"].duplicated().sum())
    df = df.drop_duplicates("open_time", keep="last").sort_values("open_time", kind="stable")
    times = df["open_time"].to_numpy()

    # 2. gaps against the grid between the first and last bar
    deltas = np.diff(times)
    for idx in np.flatnonzero(deltas != step_ms):
        first_missing = int(times[idx]) + step_ms
        last_missing = int(times[idx + 1]) - step_ms
        if last_missing < first_missing:  # misaligned bar, not a gap
            bad[_day(times[idx + 1])].add(f"misaligned:{label}")
            continue
        rep.missing_bars += (last_missing - first_missing) // step_ms + 1
        if len(rep.gaps) < MAX_LISTED_GAPS:
            rep.gaps.append((_iso(first_missing), _iso(last_missing)))
        for day_ms in range(first_missing - first_missing % _DAY_MS, last_missing + 1, _DAY_MS):
            bad[_day(day_ms)].add(f"gap:{label}")

    # 3. OHLC sanity
    o, h, lo, c, v = (df[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
    body_lo = np.minimum(o, c)
    body_hi = np.maximum(o, c)
    broken = (lo > body_lo) | (body_hi > h) | (v < 0) | ~np.isfinite(o + h + lo + c + v)
    rep.ohlc_bad = int(broken.sum())
    for t in times[broken]:
        bad[_day(t)].add(f"ohlc:{label}")
    return df


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%MZ")


def check_resample(
    m1: pd.DataFrame, h1: pd.DataFrame, tick: float | None, rep: SymbolReport, bad: dict
) -> None:
    """4. 1m resampled to 1h against native 1h, over hours with all 60 minutes."""
    if m1.empty or h1.empty:
        return
    hour = m1["open_time"] // INTERVAL_MS["1h"] * INTERVAL_MS["1h"]
    agg = (
        m1.assign(hour=hour)
        .groupby("hour")
        .agg(close=("close", "last"), volume=("volume", "sum"), n=("close", "size"))
    )
    agg = agg[agg["n"] == 60]
    joined = h1.set_index("open_time")[["close", "volume"]].join(agg, how="inner", rsuffix="_m1")
    if joined.empty:
        return
    tol = tick if tick else joined["close"].abs() * 1e-6
    close_bad = (joined["close"] - joined["close_m1"]).abs() > tol * (1 + 1e-9)
    vol_scale = joined[["volume", "volume_m1"]].abs().max(axis=1)
    vol_bad = (joined["volume"] - joined["volume_m1"]).abs() > np.maximum(vol_scale * 1e-3, 1e-9)
    rep.resample_close_bad = int(close_bad.sum())
    rep.resample_volume_bad = int(vol_bad.sum())
    for t in joined.index[close_bad]:
        bad[_day(t)].add("resample:close")
    for t in joined.index[vol_bad]:
        bad[_day(t)].add("resample:volume")


def check_mark(
    h1: pd.DataFrame, mark_h1: pd.DataFrame, max_diff: float, rep: SymbolReport, flags: dict
) -> None:
    """5. hours where the mark and last 1h closes differ by more than ``max_diff``."""
    if h1.empty or mark_h1.empty:
        return
    joined = h1.set_index("open_time")[["close"]].join(
        mark_h1.set_index("open_time")[["close"]], how="inner", rsuffix="_mark"
    )
    diff = (joined["close"] - joined["close_mark"]).abs() / joined["close_mark"]
    hits = joined.index[diff > max_diff]
    rep.mark_divergent_hours = len(hits)
    for t in hits:
        flags[_day(t)].add("mark_last")


def validate_symbol(
    paths: DataPaths,
    symbol: str,
    *,
    tick: float | None = None,
    mark_max_diff: float = 0.02,
    strict: bool = False,
) -> SymbolReport:
    rep = SymbolReport(symbol)
    bad: dict[str, set[str]] = defaultdict(set)
    flags: dict[str, set[str]] = defaultdict(set)
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for dataset in KLINE_DATASETS:
        for tf, step in INTERVAL_MS.items():
            if not paths.months(dataset, symbol, tf):
                continue
            series = SeriesReport(dataset, tf)
            df = load(paths, dataset, symbol, tf, columns=_COLS)
            target = bad if dataset == "klines" else flags
            frames[(dataset, tf)] = check_series(df, step, series, target)
            rep.series.append(series)

    empty = pd.DataFrame(columns=_COLS)
    h1 = frames.get(("klines", "1h"), empty)
    check_resample(frames.get(("klines", "1m"), empty), h1, tick, rep, bad)
    check_mark(h1, frames.get(("markPriceKlines", "1h"), empty), mark_max_diff, rep, flags)
    if strict:
        for day, reasons in flags.items():
            bad[day] |= reasons

    rep.quarantine = {d: sorted(r) for d, r in sorted(bad.items())}
    rep.flags = {d: sorted(r) for d, r in sorted(flags.items())}
    return rep
