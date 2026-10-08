"""data.binance.vision downloader for USDⓈ-M futures history.

    uv run python -m research.collect.vision coverage
    uv run python -m research.collect.vision download --kind aggTrades \\
        --symbol BTCUSDT --start 2026-09-01 --end 2026-09-30

``coverage`` answers "what history do we actually have" before anyone plans
research around it. Checked 2026-10-01 for BTCUSDT:

- ``aggTrades`` daily: current (through yesterday).
- ``bookDepth`` daily: current, but only cumulative depth at +-1..5% bands per
  snapshot, so no touch-level features.
- ``bookTicker`` daily: **ends 2024-03-30**.
- ``metrics`` daily, ``fundingRate`` monthly.

So recent best-level OFI, queue imbalance and microprice can only come from
our own recordings; vision covers trade-flow features (and best-level for
May 2023 - Mar 2024).

Every download is verified against Binance's ``.CHECKSUM`` (SHA-256).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

__all__ = ["BUCKET_URL", "DATA_URL", "coverage", "download", "list_keys", "parse_listing"]

BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA_URL = "https://data.binance.vision"
DAILY_KINDS = ("aggTrades", "bookTicker", "bookDepth", "metrics")
MONTHLY_KINDS = ("fundingRate",)
_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
_DATE = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


def prefix(kind: str, symbol: str) -> str:
    period = "monthly" if kind in MONTHLY_KINDS else "daily"
    return f"data/futures/um/{period}/{kind}/{symbol}/"


def parse_listing(xml: str) -> tuple[list[str], str | None]:
    """(zip keys, next marker or None) from one S3 ListBucket page."""
    root = ElementTree.fromstring(xml)
    keys = [node.text or "" for node in root.iterfind("s3:Contents/s3:Key", _NS)]
    truncated = (root.findtext("s3:IsTruncated", default="false", namespaces=_NS)) == "true"
    marker = root.findtext("s3:NextMarker", namespaces=_NS) if truncated else None
    if truncated and not marker and keys:
        marker = keys[-1]
    return [key for key in keys if key.endswith(".zip")], marker


def list_keys(client: httpx.Client, kind: str, symbol: str) -> list[str]:
    keys: list[str] = []
    marker: str | None = None
    while True:
        params = {"delimiter": "/", "prefix": prefix(kind, symbol)}
        if marker:
            params["marker"] = marker
        response = client.get(BUCKET_URL, params=params)
        response.raise_for_status()
        page, marker = parse_listing(response.text)
        keys += page
        if marker is None:
            return keys


def coverage(client: httpx.Client, symbols: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for kind in (*DAILY_KINDS, *MONTHLY_KINDS):
        report[kind] = {}
        for symbol in symbols:
            dates = sorted(
                m.group(1) for k in list_keys(client, kind, symbol) if (m := _DATE.search(k))
            )
            report[kind][symbol] = (
                {"first": dates[0], "last": dates[-1], "files": len(dates)} if dates else None
            )
    return report


def download(client: httpx.Client, kind: str, symbol: str, day: str, dest: Path) -> Path | None:
    """Fetch one file and verify its checksum. None when Binance has no file."""
    name = f"{symbol}-{kind}-{day}.zip"
    url = f"{DATA_URL}/{prefix(kind, symbol)}{name}"
    target = dest / kind / symbol / name
    if target.exists():
        return target
    response = client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    checksum = client.get(url + ".CHECKSUM")
    checksum.raise_for_status()
    expected = checksum.text.split()[0].lower()
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {name}: {actual} != {expected}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    return target


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data.binance.vision USDⓈ-M history")
    sub = parser.add_subparsers(dest="command", required=True)
    cov = sub.add_parser("coverage")
    cov.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    get = sub.add_parser("download")
    get.add_argument("--kind", choices=DAILY_KINDS, required=True)
    get.add_argument("--symbol", required=True)
    get.add_argument("--start", type=date.fromisoformat, required=True)
    get.add_argument("--end", type=date.fromisoformat, required=True)
    get.add_argument("--dest", type=Path, default=Path("research/data/vision"))
    args = parser.parse_args(argv)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        if args.command == "coverage":
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            print(json.dumps(coverage(client, symbols), indent=2))
            return 0
        missing = []
        for day in _days(args.start, args.end):
            path = download(client, args.kind, args.symbol.upper(), day, args.dest)
            print(f"{day} {'missing' if path is None else path}")
            if path is None:
                missing.append(day)
        return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
