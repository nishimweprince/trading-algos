"""FRED series CSV (3-month T-bill DTB3 as hurdle)."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from ..cache import RawCache
from ..config import Config
from ..http import Http


def parse_fred_csv(payload: bytes | str, series: str) -> pd.DataFrame:
    text = payload.decode() if isinstance(payload, bytes) else payload
    df = pd.read_csv(io.StringIO(text), na_values=["."], keep_default_na=True)
    date_col = "observation_date" if "observation_date" in df.columns else df.columns[0]
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], utc=True),
            series: pd.to_numeric(df[series], errors="coerce"),
        }
    )
    return out


class Fred:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "fred")
        self.series = cfg.sources.fred_series

    def sync(self) -> int:
        url = self.cfg.sources.fred_base
        r = self.http.get(url, params={"id": self.series})
        r.raise_for_status()
        df = parse_fred_csv(r.content, self.series)
        self.cache.put(f"{self.series}.csv", r.content, f"{url}?id={self.series}", 200, len(df))
        return len(df)

    def load(self) -> pd.DataFrame:
        p = self.cache.path(f"{self.series}.csv")
        if not p.exists():
            return pd.DataFrame(columns=["date", self.series])
        return parse_fred_csv(p.read_bytes(), self.series)
