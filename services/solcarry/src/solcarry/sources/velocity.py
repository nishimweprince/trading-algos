"""Velocity (ex-Drift v2 fork) SOL-PERP funding: daily CSV archive + start discovery."""

from __future__ import annotations

import io
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..cache import RawCache
from ..config import Config
from ..http import Http

log = logging.getLogger(__name__)

VELOCITY_COLS = [
    "ts",
    "txSig",
    "txSigIndex",
    "slot",
    "recordId",
    "marketIndex",
    "symbol",
    "fundingRate",
    "fundingRateLong",
    "fundingRateShort",
    "cumulativeFundingRateLong",
    "cumulativeFundingRateShort",
    "oraclePriceTwap",
    "markPriceTwap",
    "periodRevenue",
    "baseAssetAmountWithAmm",
    "baseAssetAmountWithUnsettledLp",
]
NUMERIC = [
    "fundingRate",
    "fundingRateLong",
    "fundingRateShort",
    "cumulativeFundingRateLong",
    "cumulativeFundingRateShort",
    "oraclePriceTwap",
    "markPriceTwap",
    "periodRevenue",
    "baseAssetAmountWithAmm",
    "baseAssetAmountWithUnsettledLp",
]


def parse_csv(text: str | bytes) -> pd.DataFrame:
    if isinstance(text, bytes):
        text = text.decode()
    if not text.strip():
        return pd.DataFrame(columns=VELOCITY_COLS)
    df = pd.read_csv(io.StringIO(text), dtype={"txSig": str, "recordId": str, "symbol": str})
    missing = [c for c in VELOCITY_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"velocity CSV missing columns: {missing}")
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce").astype("int64")
    df["recordId"] = pd.to_numeric(df["recordId"], errors="coerce").astype("int64")
    return df[VELOCITY_COLS]


class Velocity:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "velocity")
        self.base = cfg.sources.velocity_base.rstrip("/")
        self.symbol = cfg.velocity.symbol

    def _rel(self, d: date) -> str:
        return f"{self.symbol}/fundingRates/{d:%Y/%m/%d}.csv"

    def url(self, d: date) -> str:
        return f"{self.base}/market/{self.symbol}/fundingRates/{d:%Y/%m/%d}"

    def fetch_day(self, d: date, today: date) -> int:
        """Fetch one day (cached unless it is today). Returns row count (0 = empty)."""
        rel = self._rel(d)
        if d < today and self.cache.has(rel):
            return int((self.cache.record(rel) or {}).get("rows") or 0)
        url = self.url(d)
        r = self.http.get(url, params={"format": "csv"})
        if r.status_code == 404:
            self.cache.gap(rel, url, 404)
            return 0
        r.raise_for_status()
        rows = len(parse_csv(r.content))
        self.cache.put(rel, r.content, f"{url}?format=csv", r.status_code, rows)
        return rows

    def discover_start(self, today: date | None = None) -> date | None:
        """Walk back from today; stop after N consecutive empty days. Caches the result."""
        today = today or datetime.now(UTC).date()
        stop = self.cfg.velocity.empty_days_stop
        cached = self.cache.meta_get("start_date")
        if cached:
            return date.fromisoformat(cached)
        d = today
        empty_run = 0
        earliest: date | None = None
        while empty_run < stop:
            rows = self.fetch_day(d, today)
            if rows > 0:
                earliest = d
                empty_run = 0
            else:
                empty_run += 1
            d -= timedelta(days=1)
        if earliest is not None:
            self.cache.meta_set("start_date", earliest.isoformat())
        return earliest

    def sync(self, today: date | None = None) -> list[date]:
        today = today or datetime.now(UTC).date()
        start = self.discover_start(today)
        if start is None:
            log.warning("velocity: no data found")
            return []
        if self.cfg.run.start and self.cfg.run.start > start:
            start = self.cfg.run.start
        end = min(self.cfg.run.end or today, today)
        days = []
        d = start
        while d <= end:
            self.fetch_day(d, today)
            days.append(d)
            d += timedelta(days=1)
        return days

    def load(self) -> pd.DataFrame:
        frames = []
        for rel in sorted(self.cache.files()):
            if rel.startswith(f"{self.symbol}/fundingRates/"):
                frames.append(parse_csv(self.cache.path(rel).read_bytes()))
        if not frames:
            return pd.DataFrame(columns=VELOCITY_COLS)
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(subset=["recordId"]).sort_values("ts").reset_index(drop=True)

    def fetch_recent_json(self) -> pd.DataFrame:
        """Rolling ~31 days endpoint (cross-check only)."""
        r = self.http.get(f"{self.base}/market/{self.symbol}/fundingRates")
        r.raise_for_status()
        js = r.json()
        return pd.DataFrame(js.get("records", []))
