"""Download Binance spot USDT daily klines (monthly zips) for every listed or delisted pair.

    uv run python -m crypto_trend.fetch [--out DIR] [--workers 16]

Files land in ``DIR/<SYMBOL>/<SYMBOL>-1d-YYYY-MM.zip`` and are verified against the MD5 ETag
in the archive's S3 listing. Zips are read in place with ``zipfile``; nothing is extracted.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILES = "https://data.binance.vision"
DEFAULT_OUT = Path("/Users/pnishimwe/Documents/Markets/Data/binance-spot-1d")

EXCLUDED_BASES = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDD", "AEUR", "EUR", "GBP", "AUD",
    "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "BIDR", "IDRT", "BVND", "PLN", "RON", "ARS", "JPY",
    "MXN", "COP", "CZK", "EURI", "XUSD", "USD1", "RLUSD", "USDE", "PYUSD", "PAXG", "XAUT",
    "USDS", "USDSB", "SUSD", "UST", "USTC",
}
LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR)$")


def eligible_symbol(sym: str) -> bool:
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    return bool(base) and base not in EXCLUDED_BASES and not LEVERAGED.search(base)


def _get(url: str) -> bytes:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed https hosts)
                return r.read()
        except OSError:
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _list(prefix: str, delimiter: bool) -> tuple[list[str], list[tuple[str, str]]]:
    """(common prefixes, [(key, etag)]) for an S3 prefix, following pagination."""
    prefixes, keys, marker = [], [], ""
    while True:
        q = {"prefix": prefix, "marker": marker}
        if delimiter:
            q["delimiter"] = "/"
        xml = _get(f"{S3}?{urllib.parse.urlencode(q)}").decode()
        prefixes += re.findall(r"<Prefix>([^<]+)</Prefix>", xml)[1:] if delimiter else []
        keys += re.findall(r"<Key>([^<]+)</Key>.*?<ETag>&quot;([0-9a-f]+)&quot;</ETag>", xml,
                           flags=re.S)
        if "<IsTruncated>true" not in xml:
            return prefixes, keys
        last = re.findall(r"<NextMarker>([^<]+)</NextMarker>", xml) or [
            (prefixes[-1] if delimiter and prefixes else keys[-1][0])]
        marker = last[-1]


def list_symbols() -> list[str]:
    prefixes, _ = _list("data/spot/monthly/klines/", delimiter=True)
    syms = sorted({p.rstrip("/").split("/")[-1] for p in prefixes})
    return [s for s in syms if eligible_symbol(s)]


def _fetch_symbol(sym: str, out: Path) -> int:
    _, keys = _list(f"data/spot/monthly/klines/{sym}/1d/", delimiter=False)
    n = 0
    for key, etag in keys:
        if not key.endswith(".zip"):
            continue
        dest = out / sym / key.split("/")[-1]
        if dest.exists() and hashlib.md5(dest.read_bytes()).hexdigest() == etag:  # noqa: S324
            n += 1
            continue
        data = _get(f"{FILES}/{urllib.parse.quote(key)}")  # some symbols are non-ASCII
        if hashlib.md5(data).hexdigest() != etag:  # noqa: S324 (integrity, not security)
            raise ValueError(f"MD5 mismatch for {key}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        n += 1
    return n


def fetch(out: Path, workers: int = 16) -> dict[str, int]:
    syms = list_symbols()
    print(f"{len(syms)} eligible USDT pairs")
    with ThreadPoolExecutor(workers) as ex:
        counts = dict(zip(syms, ex.map(lambda s: _fetch_symbol(s, out), syms), strict=True))
    print(f"{sum(counts.values())} monthly files OK under {out}")
    return counts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args(argv)
    fetch(Path(a.out), a.workers)


if __name__ == "__main__":
    main()
