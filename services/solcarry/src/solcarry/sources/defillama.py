"""DeFiLlama yields API: daily APY history per pool, plus pool discovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..cache import RawCache
from ..config import Config
from ..http import Http


def parse_chart(payload: bytes | str | dict[str, Any]) -> pd.DataFrame:
    js = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
    data = js.get("data", [])
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "apy", "apyBase", "tvlUsd"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ("apy", "apyBase", "tvlUsd"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


class DefiLlama:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "defillama")
        self.base = cfg.sources.defillama_base.rstrip("/")

    def pools(self) -> dict[str, str]:
        return {
            "jitosol": self.cfg.staking.lst_pool_id,
            "aave_usdc": self.cfg.lending.aave_usdc_pool_id,
            "kamino_usdc": self.cfg.lending.kamino_usdc_pool_id,
        }

    def fetch_chart(self, name: str, pool_id: str) -> pd.DataFrame:
        url = f"{self.base}/chart/{pool_id}"
        r = self.http.get(url)
        rel = f"chart/{name}_{pool_id}.json"
        if r.status_code != 200:
            self.cache.gap(rel, url, r.status_code)
            return parse_chart({"data": []})
        df = parse_chart(r.content)
        self.cache.put(rel, r.content, url, r.status_code, len(df))
        return df

    def sync(self) -> dict[str, int]:
        return {n: len(self.fetch_chart(n, pid)) for n, pid in self.pools().items()}

    def load(self, name: str) -> pd.DataFrame:
        pid = self.pools()[name]
        p = self.cache.path(f"chart/{name}_{pid}.json")
        if not p.exists():
            return parse_chart({"data": []})
        return parse_chart(p.read_bytes())

    def discover_pool(self, project: str, symbol: str) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.base}/pools")
        r.raise_for_status()
        rows: list[dict[str, Any]] = r.json().get("data", [])
        return [
            p
            for p in rows
            if p.get("project") == project and str(p.get("symbol", "")).upper() == symbol.upper()
        ]
