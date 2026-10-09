"""data.binance.vision access for USDⓈ-M futures history.

Adapted from ``services/ofi-scalper-service/research/collect/vision.py`` (S3
listing plus SHA-256 ``.CHECKSUM`` verification), extended with the interval
path segment that kline datasets carry and with a month planner:

- closed months come from the ``monthly`` archive;
- a month with no monthly archive yet (the current month, and the previous one
  for a few days after it ends) is assembled from ``daily`` archives.

Every downloaded zip is verified against its checksum before it is written,
and written atomically, so a file on disk is always a verified file.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import httpx

__all__ = [
    "BUCKET_URL",
    "DATA_URL",
    "DATASETS",
    "ChecksumError",
    "MonthPlan",
    "download",
    "list_keys",
    "list_symbols",
    "parse_listing",
    "plan_months",
    "prefix",
]

BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA_URL = "https://data.binance.vision"
ROOT = "data/futures/um"

# dataset -> whether its paths carry an interval segment
DATASETS: dict[str, bool] = {
    "klines": True,
    "markPriceKlines": True,
    "indexPriceKlines": True,
    "fundingRate": False,
}
# fundingRate is published monthly only.
MONTHLY_ONLY = {"fundingRate"}

_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
_MONTH = re.compile(r"-(\d{4}-\d{2})\.zip$")
_DAY = re.compile(r"-(\d{4}-\d{2})-(\d{2})\.zip$")


class ChecksumError(ValueError):
    pass


RETRIES = 5
BACKOFF_S = 2.0


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with retries on timeouts, connection errors and 5xx (the S3 bucket throttles)."""
    for attempt in range(RETRIES):
        try:
            response = client.get(url, **kw)
            if response.status_code < 500:
                return response
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == RETRIES - 1:
                raise
        time.sleep(BACKOFF_S * 2**attempt)
    return response


def prefix(dataset: str, symbol: str, interval: str | None, period: str) -> str:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    if DATASETS[dataset] != (interval is not None):
        raise ValueError(f"{dataset}: interval {'required' if DATASETS[dataset] else 'not used'}")
    tail = f"{interval}/" if interval else ""
    return f"{ROOT}/{period}/{dataset}/{symbol}/{tail}"


def parse_listing(xml: str) -> tuple[list[str], list[str], str | None]:
    """(keys, common prefixes, next marker or None) from one S3 ListBucket page."""
    root = ElementTree.fromstring(xml)
    keys = [n.text or "" for n in root.iterfind("s3:Contents/s3:Key", _NS)]
    prefixes = [n.text or "" for n in root.iterfind("s3:CommonPrefixes/s3:Prefix", _NS)]
    truncated = root.findtext("s3:IsTruncated", default="false", namespaces=_NS) == "true"
    marker = root.findtext("s3:NextMarker", namespaces=_NS) if truncated else None
    if truncated and not marker:
        tail = keys + prefixes
        marker = max(tail) if tail else None
    return keys, prefixes, marker


def _list(client: httpx.Client, pfx: str) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    prefixes: list[str] = []
    marker: str | None = None
    while True:
        params = {"delimiter": "/", "prefix": pfx}
        if marker:
            params["marker"] = marker
        response = _get(client, BUCKET_URL, params=params)
        response.raise_for_status()
        page_keys, page_prefixes, marker = parse_listing(response.text)
        keys += page_keys
        prefixes += page_prefixes
        if marker is None:
            return keys, prefixes


def list_keys(client: httpx.Client, pfx: str) -> list[str]:
    """Every ``.zip`` key directly under ``pfx`` (checksums excluded)."""
    return [k for k in _list(client, pfx)[0] if k.endswith(".zip")]


def list_symbols(client: httpx.Client) -> list[str]:
    """Every USDⓈ-M symbol with monthly klines, delisted ones included."""
    pfx = f"{ROOT}/monthly/klines/"
    _, prefixes = _list(client, pfx)
    return sorted(p.removeprefix(pfx).rstrip("/") for p in prefixes if p != pfx)


@dataclass(frozen=True)
class MonthPlan:
    month: str  # YYYY-MM
    source: str  # "monthly" | "daily"
    keys: tuple[str, ...]


def plan_months(
    client: httpx.Client,
    dataset: str,
    symbol: str,
    interval: str | None,
    start: str,
    end: str | None = None,
) -> list[MonthPlan]:
    """The archives that cover each month from ``start`` to ``end`` (YYYY-MM, inclusive).

    Months before a symbol's listing simply have no archives and are absent
    from the plan.
    """
    monthly = {
        m.group(1): k
        for k in list_keys(client, prefix(dataset, symbol, interval, "monthly"))
        if (m := _MONTH.search(k))
    }
    daily: dict[str, list[str]] = {}
    if dataset not in MONTHLY_ONLY:
        for k in list_keys(client, prefix(dataset, symbol, interval, "daily")):
            if (m := _DAY.search(k)) and m.group(1) not in monthly:
                daily.setdefault(m.group(1), []).append(k)
    plans = [MonthPlan(month, "monthly", (key,)) for month, key in monthly.items()]
    plans += [MonthPlan(month, "daily", tuple(sorted(keys))) for month, keys in daily.items()]
    return sorted(
        (p for p in plans if p.month >= start and (end is None or p.month <= end)),
        key=lambda p: p.month,
    )


def download(client: httpx.Client, key: str, raw_root: Path) -> Path:
    """Fetch one archive, verify it against ``.CHECKSUM``, and write it atomically.

    An existing file is returned as is: it was verified when it was written.
    """
    target = raw_root / key
    if target.exists():
        return target
    url = f"{DATA_URL}/{key}"
    response = _get(client, url)
    response.raise_for_status()
    checksum = _get(client, url + ".CHECKSUM")
    checksum.raise_for_status()
    expected = checksum.text.split()[0].lower()
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != expected:
        raise ChecksumError(f"checksum mismatch for {key}: {actual} != {expected}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(response.content)
    tmp.replace(target)
    return target
