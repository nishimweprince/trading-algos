"""Binance public archive (data.binance.vision): S3 listing, zip download, checksum verify.

Binance REST is geo-blocked from several locations (HTTP 451/403), so all Binance
data comes from the archive.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from ..cache import RawCache, sha256_bytes
from ..config import Config
from ..http import Http

log = logging.getLogger(__name__)

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
KLINE_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
FUNDING_COLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
MONTH_RE = re.compile(r"-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip$")


class ChecksumError(RuntimeError):
    pass


def parse_s3_listing(xml_text: str) -> tuple[list[str], list[str], bool, str | None]:
    """Return (keys, common_prefixes, is_truncated, next_marker)."""
    root = ET.fromstring(xml_text)
    keys = [el.text or "" for el in root.iter(f"{S3_NS}Key")]
    common = [(cp.findtext(f"{S3_NS}Prefix") or "") for cp in root.iter(f"{S3_NS}CommonPrefixes")]
    trunc = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
    nxt = root.findtext(f"{S3_NS}NextMarker")
    if trunc and not nxt:
        nxt = keys[-1] if keys else (common[-1] if common else None)
    return keys, common, trunc, nxt


def parse_checksum(text: str) -> str:
    return text.strip().split()[0].lower()


def quarterly_expiry(symbol: str, expiry_hour_utc: int) -> pd.Timestamp:
    m = re.search(r"_(\d{6})$", symbol)
    if not m:
        raise ValueError(f"not a dated contract: {symbol}")
    d = datetime.strptime(m.group(1), "%y%m%d").replace(hour=expiry_hour_utc, tzinfo=UTC)
    return pd.Timestamp(d)


def read_zip_csv(payload: bytes, columns: list[str]) -> pd.DataFrame:
    """Read the single CSV in a Binance zip. Header row is optional (older files lack it)."""
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        raw = zf.read(name)
    first = raw.split(b"\n", 1)[0].split(b",")[0].strip()
    has_header = not first.replace(b".", b"").replace(b"-", b"").isdigit()
    df = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)
    if not has_header:
        df.columns = pd.Index(columns[: len(df.columns)])
    else:
        df.columns = pd.Index([str(c).strip() for c in df.columns])
    return df


@dataclass(frozen=True)
class Dataset:
    name: str
    monthly_prefix: str  # under data/, ends with '/'
    daily_prefix: str | None
    columns: list[str]


def datasets_for(cfg: Config, quarterlies: Iterable[str]) -> list[Dataset]:
    perp = cfg.binance.perp_symbol
    pair = cfg.binance.coinm_pair
    out = [
        Dataset(
            "funding",
            f"futures/um/monthly/fundingRate/{perp}/",
            None,
            FUNDING_COLS,
        ),
        Dataset(
            "spot",
            f"spot/monthly/klines/{perp}/1h/",
            f"spot/daily/klines/{perp}/1h/",
            KLINE_COLS,
        ),
        Dataset(
            "index",
            f"futures/cm/monthly/indexPriceKlines/{pair}/1h/",
            f"futures/cm/daily/indexPriceKlines/{pair}/1h/",
            KLINE_COLS,
        ),
    ]
    for q in quarterlies:
        out.append(
            Dataset(
                f"quarterly:{q}",
                f"futures/cm/monthly/klines/{q}/1h/",
                f"futures/cm/daily/klines/{q}/1h/",
                KLINE_COLS,
            )
        )
    return out


class BinanceVision:
    def __init__(self, cfg: Config, http: Http, raw_root: Path) -> None:
        self.cfg = cfg
        self.http = http
        self.cache = RawCache(raw_root, "binance")
        self.base = cfg.sources.binance_vision_base.rstrip("/")
        self.s3 = cfg.sources.binance_s3_list.rstrip("/")

    # ---- listing -------------------------------------------------------
    def list_prefix(self, prefix: str) -> tuple[list[str], list[str]]:
        """List keys and common prefixes under ``data/<prefix>`` (paginated)."""
        full = "data/" + prefix
        keys: list[str] = []
        common: list[str] = []
        marker: str | None = None
        while True:
            params = {"delimiter": "/", "prefix": full}
            if marker:
                params["marker"] = marker
            r = self.http.get(self.s3, params=params)
            r.raise_for_status()
            k, c, trunc, marker = parse_s3_listing(r.text)
            keys += k
            common += c
            if not trunc or not marker:
                break
        return keys, common

    def discover_quarterlies(self) -> list[str]:
        _, common = self.list_prefix("futures/cm/monthly/klines/")
        pair = self.cfg.binance.coinm_pair
        rx = re.compile(rf"/({re.escape(pair)}_(\d{{6}}))/$")
        syms = sorted({m.group(1) for p in common if (m := rx.search(p))})
        self.cache.meta_set("quarterlies", syms)
        return syms

    # ---- download ------------------------------------------------------
    def _in_range(self, key: str) -> bool:
        m = MONTH_RE.search(key)
        if not m:
            return False
        y, mo = int(m.group(1)), int(m.group(2))
        start, end = self.cfg.run.start, self.cfg.run.end
        if start and (y, mo) < (start.year, start.month):
            return False
        if end and (y, mo) > (end.year, end.month):
            return False
        return True

    def download(self, key: str) -> tuple[str, int | None]:
        """Download one zip + .CHECKSUM, verify, cache. Returns (relpath, rows)."""
        rel = key.removeprefix("data/")
        if self.cache.has(rel):
            rec = self.cache.record(rel) or {}
            return rel, rec.get("rows")
        url = f"{self.base}/{rel}"
        r = self.http.get(url)
        if r.status_code == 404:
            self.cache.gap(rel, url, 404, "not published")
            return rel, None
        r.raise_for_status()
        payload = r.content
        cr = self.http.get(url + ".CHECKSUM")
        expected: str | None = None
        if cr.status_code == 200:
            expected = parse_checksum(cr.text)
            got = sha256_bytes(payload)
            if got != expected:
                raise ChecksumError(f"checksum mismatch for {url}: got {got}, want {expected}")
        else:
            log.warning("no CHECKSUM for %s (HTTP %s)", url, cr.status_code)
        rows: int | None = None
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                name = next(n for n in zf.namelist() if n.endswith(".csv"))
                rows = zf.read(name).count(b"\n")
        except (zipfile.BadZipFile, StopIteration) as e:
            raise ChecksumError(f"bad zip from {url}: {e}") from e
        self.cache.put(rel, payload, url, r.status_code, rows, {"checksum": expected})
        return rel, rows

    def sync(self, ds: Dataset, today: date | None = None) -> list[str]:
        today = today or datetime.now(UTC).date()
        keys, _ = self.list_prefix(ds.monthly_prefix)
        zips = [k for k in keys if k.endswith(".zip") and self._in_range(k)]
        months_present = {MONTH_RE.search(k).group(1, 2) for k in zips}  # type: ignore[union-attr]
        cur = (f"{today.year:04d}", f"{today.month:02d}")
        if cur not in months_present and self._month_wanted(today):
            rel = f"{ds.monthly_prefix}{_stem(ds.monthly_prefix)}-{cur[0]}-{cur[1]}.zip"
            self.cache.gap(rel, f"{self.base}/{rel}", 404, "current month not yet published")
            if ds.daily_prefix and self.cfg.binance.include_current_month_daily:
                dkeys, _ = self.list_prefix(
                    f"{ds.daily_prefix}{_stem(ds.daily_prefix)}-{cur[0]}-{cur[1]}"
                )
                zips += [k for k in dkeys if k.endswith(".zip")]
        with ThreadPoolExecutor(max_workers=self.cfg.http.max_parallel_per_host) as ex:
            done = list(ex.map(self.download, zips))
        return [rel for rel, rows in done if rows is not None]

    def _month_wanted(self, today: date) -> bool:
        end = self.cfg.run.end
        return end is None or (end.year, end.month) >= (today.year, today.month)

    def expiry_minute_keys(self, sym: str) -> tuple[str, str]:
        """Archive keys of the 1m contract and 1m index files covering ``sym``'s expiry month."""
        exp = quarterly_expiry(sym, self.cfg.binance.expiry_hour_utc)
        ym = f"{exp:%Y-%m}"
        pair = self.cfg.binance.coinm_pair
        return (
            f"data/futures/cm/monthly/klines/{sym}/1m/{sym}-1m-{ym}.zip",
            f"data/futures/cm/monthly/indexPriceKlines/{pair}/1m/{pair}-1m-{ym}.zip",
        )

    def sync_expiry_minutes(
        self, quarterlies: Iterable[str], now: datetime | None = None
    ) -> list[str]:
        """1m klines for the expiry month of each expired contract (convergence validation)."""
        now = now or datetime.now(UTC)
        keys: list[str] = []
        for q in quarterlies:
            if quarterly_expiry(q, self.cfg.binance.expiry_hour_utc) <= pd.Timestamp(now):
                keys += [k for k in self.expiry_minute_keys(q) if k not in keys]
        with ThreadPoolExecutor(max_workers=self.cfg.http.max_parallel_per_host) as ex:
            done = list(ex.map(self.download, keys))
        return [rel for rel, rows in done if rows is not None]

    def load_key(self, key: str) -> pd.DataFrame:
        rel = key.removeprefix("data/")
        p = self.cache.path(rel)
        if not self.cache.record(rel) or not p.exists():
            return pd.DataFrame(columns=KLINE_COLS)
        return read_zip_csv(p.read_bytes(), KLINE_COLS)

    def sync_all(self) -> dict[str, list[str]]:
        qs = self.discover_quarterlies()
        out: dict[str, list[str]] = {}
        for ds in datasets_for(self.cfg, qs):
            log.info("binance: syncing %s", ds.name)
            out[ds.name] = self.sync(ds)
        log.info("binance: syncing 1m expiry windows")
        out["expiry_1m"] = self.sync_expiry_minutes(qs)
        return out

    # ---- read back -----------------------------------------------------
    def load(self, ds: Dataset) -> pd.DataFrame:
        frames = []
        for rel in sorted(self.cache.files()):
            if rel.startswith(ds.monthly_prefix) or (
                ds.daily_prefix is not None and rel.startswith(ds.daily_prefix)
            ):
                frames.append(read_zip_csv(self.cache.path(rel).read_bytes(), ds.columns))
        if not frames:
            return pd.DataFrame(columns=ds.columns)
        return pd.concat(frames, ignore_index=True)

    def known_quarterlies(self) -> list[str]:
        q = self.cache.meta_get("quarterlies")
        return list(q) if q else []


def _stem(prefix: str) -> str:
    """'spot/monthly/klines/SOLUSDT/1h/' -> 'SOLUSDT-1h'; funding -> 'SOLUSDT-fundingRate'."""
    parts = [p for p in prefix.split("/") if p]
    if parts[-1] == "1h":
        return f"{parts[-2]}-1h"
    return f"{parts[-1]}-{parts[-2]}"
