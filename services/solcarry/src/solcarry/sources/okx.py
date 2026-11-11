"""OKX recent funding history (optional cross-check, ~3 months only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..cache import RawCache
from ..config import Config
from ..http import Http


def parse_okx(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "fundingRate", "realizedRate"])
    df = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "ts": pd.to_datetime(pd.to_numeric(df["fundingTime"]), unit="ms", utc=True),
            "fundingRate": pd.to_numeric(df["fundingRate"], errors="coerce"),
            "realizedRate": pd.to_numeric(df["realizedRate"], errors="coerce")
            if "realizedRate" in df.columns
            else np.nan,
        }
    )
    return out.sort_values("ts").reset_index(drop=True)


class Okx:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "okx")

    def sync(self, inst: str = "SOL-USDT-SWAP", max_pages: int = 30) -> int:
        url = f"{self.cfg.sources.okx_base}/api/v5/public/funding-rate-history"
        rows: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"instId": inst, "limit": 100}
            if after:
                params["after"] = after
            r = self.http.get(url, params=params)
            if r.status_code != 200:
                break
            data = r.json().get("data", [])
            if not data:
                break
            rows += data
            after = data[-1]["fundingTime"]
        payload = json.dumps({"data": rows}).encode()
        self.cache.put(f"{inst}.json", payload, url, 200, len(rows))
        return len(rows)

    def load(self, inst: str = "SOL-USDT-SWAP") -> pd.DataFrame:
        p = self.cache.path(f"{inst}.json")
        if not p.exists():
            return parse_okx([])
        return parse_okx(json.loads(p.read_bytes())["data"])
