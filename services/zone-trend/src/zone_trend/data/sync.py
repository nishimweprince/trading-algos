"""Download Vision archives and convert them into Parquet month files.

A month is (re)built when its file is missing, when it was built from daily
archives and either the monthly archive now exists or more daily archives do.
A month built from the monthly archive is final and is never touched again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import pandas as pd

from zone_trend.data import vision
from zone_trend.data.parse import read_archive
from zone_trend.data.store import EVENT_TF, DataPaths, month_meta, write_month

__all__ = ["DEFAULT_INTERVALS", "SyncResult", "needs_build", "sync_series", "sync"]

log = logging.getLogger(__name__)

DEFAULT_INTERVALS: dict[str, tuple[str, ...]] = {
    "klines": ("1m", "1h", "4h", "1d"),
    "markPriceKlines": ("1m", "1h"),
    "indexPriceKlines": ("1h",),
    "fundingRate": (),
}


@dataclass
class SyncResult:
    dataset: str
    symbol: str
    tf: str
    built: list[str]
    skipped: int
    error: str | None = None


def needs_build(meta: dict | None, plan: vision.MonthPlan) -> bool:
    if meta is None or meta.get("source") == "unknown":
        return True
    if meta["source"] == "monthly":
        return False
    return plan.source == "monthly" or len(plan.keys) > meta.get("files", 0)


def _dedupe(df: pd.DataFrame, key: str) -> pd.DataFrame:
    # Overlapping daily files can repeat a row; keep the last copy (validate logs it).
    return df.drop_duplicates(subset=key, keep="last").sort_values(key, kind="stable")


def sync_series(
    client: httpx.Client,
    paths: DataPaths,
    dataset: str,
    symbol: str,
    interval: str | None,
    start: str,
    end: str | None = None,
    keep_raw: bool = True,
) -> SyncResult:
    tf = interval or EVENT_TF
    built: list[str] = []
    skipped = 0
    for plan in vision.plan_months(client, dataset, symbol, interval, start, end):
        target = paths.month_file(dataset, symbol, tf, plan.month)
        if not needs_build(month_meta(target), plan):
            skipped += 1
            continue
        frames = []
        for key in plan.keys:
            zpath = vision.download(client, key, paths.raw)
            frames.append(read_archive(zpath, dataset))
            if not keep_raw:
                zpath.unlink()
        df = pd.concat(frames, ignore_index=True)
        df = _dedupe(df, "settle_time" if dataset == "fundingRate" else "open_time")
        write_month(target, df, source=plan.source, files=len(plan.keys))
        built.append(plan.month)
    return SyncResult(dataset, symbol, tf, built, skipped)


def sync(
    paths: DataPaths,
    symbols: list[str],
    datasets: dict[str, tuple[str, ...]],
    start: str,
    end: str | None = None,
    workers: int = 8,
    keep_raw: bool = True,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> list[SyncResult]:
    """Sync every (dataset, interval, symbol) series; one failure never stops the rest."""
    jobs = [
        (dataset, symbol, interval)
        for dataset, intervals in datasets.items()
        for interval in (intervals or (None,))
        for symbol in symbols
    ]
    factory = client_factory or (
        lambda: httpx.Client(timeout=120, follow_redirects=True)
    )

    with factory() as client:

        def run(job: tuple[str, str, str | None]) -> SyncResult:
            dataset, symbol, interval = job
            try:
                return sync_series(
                    client, paths, dataset, symbol, interval, start, end, keep_raw
                )
            except (httpx.HTTPError, vision.ChecksumError, ValueError, OSError) as exc:
                log.warning("sync failed: %s %s %s: %s", dataset, symbol, interval, exc)
                return SyncResult(dataset, symbol, interval or EVENT_TF, [], 0, str(exc))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(run, jobs))
