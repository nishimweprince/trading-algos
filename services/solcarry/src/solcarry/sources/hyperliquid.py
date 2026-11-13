"""Hyperliquid perp funding history via the public ``/info`` endpoint (``fundingHistory``).

One raw JSON file per calendar month (``<coin>/fundingHistory/YYYY-MM.json``). Past months
are fetched once; the current month is refetched on every sync. Each record is an hourly
payment: ``fundingRate`` is the fraction of notional paid by longs to shorts that hour, so
positive = the short receives (our sign convention, no flip).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..cache import RawCache
from ..config import Config
from ..http import Http

log = logging.getLogger(__name__)

HL_COLS = ["time", "coin", "fundingRate", "premium"]


def parse_funding(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=HL_COLS)
    df = pd.DataFrame(rows)
    missing = [c for c in ("time", "fundingRate") if c not in df.columns]
    if missing:
        raise ValueError(f"hyperliquid fundingHistory missing fields: {missing}")
    out = pd.DataFrame(
        {
            "time": pd.to_numeric(df["time"], errors="coerce").astype("int64"),
            "coin": df.get("coin", pd.Series([""] * len(df))).astype(str),
            "fundingRate": pd.to_numeric(df["fundingRate"], errors="coerce"),
            "premium": pd.to_numeric(df["premium"], errors="coerce")
            if "premium" in df.columns
            else float("nan"),
        }
    )
    return out.sort_values("time").reset_index(drop=True)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


class Hyperliquid:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "hyperliquid")
        self.url = cfg.sources.hyperliquid_info
        self.coin = cfg.hyperliquid.coin
        self._last = 0.0

    def _rel(self, month: date) -> str:
        return f"{self.coin}/fundingHistory/{month:%Y-%m}.json"

    def _post(self, start_ms: int, end_ms: int | None = None) -> list[dict[str, Any]]:
        wait = self.cfg.hyperliquid.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        body: dict[str, Any] = {"type": "fundingHistory", "coin": self.coin, "startTime": start_ms}
        if end_ms is not None:
            body["endTime"] = end_ms
        r = self.http.post_json(self.url, body)
        self._last = time.monotonic()
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"hyperliquid: unexpected response {str(data)[:200]}")
        return data

    def fetch_range(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """All records in [start_ms, end_ms], paging by the last timestamp."""
        rows: list[dict[str, Any]] = []
        st = start_ms
        limit = self.cfg.hyperliquid.page_limit
        while st <= end_ms:
            page = self._post(st, end_ms)
            if not page:
                break
            rows += page
            if len(page) < limit:
                break
            st = int(page[-1]["time"]) + 1
        return rows

    def discover_start(self) -> date | None:
        cached = self.cache.meta_get("start_month")
        if cached:
            return date.fromisoformat(cached)
        first = self._post(0)
        if not first:
            return None
        t0 = datetime.fromtimestamp(int(first[0]["time"]) / 1000, tz=UTC).date()
        start = date(t0.year, t0.month, 1)
        self.cache.meta_set("start_month", start.isoformat())
        return start

    def sync(self, today: date | None = None) -> int:
        """Fetch missing months; returns the number of months (re)fetched."""
        today = today or datetime.now(UTC).date()
        start = self.discover_start()
        if start is None:
            log.warning("hyperliquid: no %s funding history", self.coin)
            return 0
        if self.cfg.run.start and self.cfg.run.start > start:
            start = date(self.cfg.run.start.year, self.cfg.run.start.month, 1)
        end = min(self.cfg.run.end or today, today)
        cur = date(today.year, today.month, 1)
        fetched = 0
        m = start
        while m <= end:
            rel = self._rel(m)
            if m < cur and self.cache.has(rel):
                m = _next_month(m)
                continue
            rows = self.fetch_range(_ms(m), _ms(_next_month(m)) - 1)
            self.cache.put(
                rel,
                json.dumps(rows).encode(),
                f"{self.url}#fundingHistory/{self.coin}/{m:%Y-%m}",
                200,
                len(rows),
            )
            fetched += 1
            m = _next_month(m)
        return fetched

    def load(self) -> pd.DataFrame:
        frames = []
        prefix = f"{self.coin}/fundingHistory/"
        for rel in sorted(self.cache.files()):
            if rel.startswith(prefix):
                frames.append(parse_funding(json.loads(self.cache.path(rel).read_bytes())))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=HL_COLS)
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
