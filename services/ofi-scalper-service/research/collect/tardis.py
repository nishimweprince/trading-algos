"""Tardis.dev downloader for Binance USDⓈ-M history (``binance-futures``).

    uv run python -m research.collect.tardis info
    uv run python -m research.collect.tardis download --start 2026-09-01 --end 2026-09-10

Tardis has what data.binance.vision lacks after March 2024: the full
event-level book (``incremental_book_L2``, every ``depth@0ms`` update, with a
snapshot at each day's start) plus 25-level snapshots, trades, funding/mark
(``derivative_ticker``) and liquidations.

The key is ``TARDIS_API_KEY`` (environment, or ``.env.<profile>``). ``info``
shows what it may fetch; the trial key from 2026-10-06 covers
binance-futures 2026-09-01 to 2026-09-11 (exclusive).

Files land in ``research/data/tardis/<data_type>/<SYMBOL>/<YYYY-MM-DD>.csv.gz``.
Each is streamed to a ``.part`` file, checked to be a complete gzip, then
renamed, so an existing file is always whole and is skipped on a rerun.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import zlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

__all__ = ["API_URL", "DATASETS_URL", "api_key", "download", "key_info"]

API_URL = "https://api.tardis.dev/v1"
DATASETS_URL = "https://datasets.tardis.dev/v1"
EXCHANGE = "binance-futures"
DATA_TYPES = (
    "incremental_book_L2",
    "book_snapshot_25",
    "trades",
    "derivative_ticker",
    "liquidations",
)
_CHUNK = 1 << 20
_RETRY_BACKOFF_SECONDS = (15, 30, 60, 120, 300)


def api_key(profile: str) -> str | None:
    """TARDIS_API_KEY from the environment, else from ``.env.<profile>``."""
    if key := os.environ.get("TARDIS_API_KEY", "").strip():
        return key
    env_file = Path(f".env.{profile}")
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "TARDIS_API_KEY":
            return value.strip().strip("\"'") or None
    return None


def key_info(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(f"{API_URL}/api-key-info")
    response.raise_for_status()
    return response.json()


def _gzip_complete(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(_CHUNK):
                pass
    except (OSError, EOFError, zlib.error):
        return False
    return True


def _fetch(client: httpx.Client, url: str, part: Path) -> bool:
    """Stream ``url`` into ``part``. False when the key may not read it or it does not exist."""
    with client.stream("GET", url) as response:
        if response.status_code in (403, 404):
            return False
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_bytes(_CHUNK):
                handle.write(chunk)
    return True


def download(
    client: httpx.Client, data_type: str, symbol: str, day: date, dest: Path
) -> Path | None:
    """Fetch one symbol-day. None when the key may not read it (403) or it does not exist.

    Server errors (5xx), dropped connections and truncated files are retried
    with backoff; Tardis builds a day's file on first request and can 500 then.
    """
    target = dest / data_type / symbol / f"{day.isoformat()}.csv.gz"
    if target.exists():
        return target
    url = f"{DATASETS_URL}/{EXCHANGE}/{data_type}/{day:%Y/%m/%d}/{symbol}.csv.gz"
    part = target.with_suffix(".gz.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt, backoff in enumerate((*_RETRY_BACKOFF_SECONDS, None), start=1):
        try:
            if not _fetch(client, url, part):
                return None
            if _gzip_complete(part):
                part.rename(target)
                return target
            error: Exception = ValueError("truncated gzip")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            error = exc
        except httpx.TransportError as exc:
            error = exc
        part.unlink(missing_ok=True)
        if backoff is None:
            message = f"{data_type} {symbol} {day}: gave up after {attempt} tries"
            raise RuntimeError(message) from error
        print(f"{day} {data_type} {symbol} retry {attempt} in {backoff}s: {error}", flush=True)
        time.sleep(backoff)
    raise AssertionError("unreachable")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tardis.dev binance-futures history")
    parser.add_argument("--profile", default="dev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    get = sub.add_parser("download")
    get.add_argument("--start", type=date.fromisoformat, required=True)
    get.add_argument("--end", type=date.fromisoformat, required=True, help="inclusive")
    get.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    get.add_argument("--data-types", default=",".join(DATA_TYPES))
    get.add_argument("--dest", type=Path, default=Path("research/data/tardis"))
    args = parser.parse_args(argv)

    key = api_key(args.profile)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout = httpx.Timeout(60, read=300)
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        if args.command == "info":
            if not key:
                print("no TARDIS_API_KEY", file=sys.stderr)
                return 1
            ours = [e for e in key_info(client) if e.get("exchange") == EXCHANGE]
            print(json.dumps(ours, indent=2))
            return 0
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        data_types = [t.strip() for t in args.data_types.split(",") if t.strip()]
        missing = 0
        for day in _days(args.start, args.end):
            for data_type in data_types:
                for symbol in symbols:
                    path = download(client, data_type, symbol, day, args.dest)
                    if path is None:
                        missing += 1
                        print(f"{day} {data_type} {symbol} unavailable", flush=True)
                    else:
                        mb = path.stat().st_size / 1e6
                        print(f"{day} {data_type} {symbol} {mb:.1f} MB", flush=True)
        return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
