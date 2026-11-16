"""Download Binance USD-M perpetual 5-minute klines (monthly zips) with checksum checks.

    uv run python -m crypto_intraday.fetch [--out DIR] [--start 2022-01] [--end 2026-10]

Files land in ``DIR/<SYMBOL>/<SYMBOL>-5m-YYYY-MM.zip``. Zips are kept as downloaded and read
with ``zipfile`` directly; nothing is extracted or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import time
import urllib.request
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEFAULT_OUT = Path("/Users/pnishimwe/Documents/Markets/Data/binance-um-5m")


def months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ye, me = map(int, end.split("-"))
    out = []
    while (y, m) <= (ye, me):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed https host)
                return r.read()
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch(out: Path, start: str, end: str, symbols=SYMBOLS) -> list[Path]:
    got = []
    for sym in symbols:
        d = out / sym
        d.mkdir(parents=True, exist_ok=True)
        for ym in months(start, end):
            name = f"{sym}-5m-{ym}.zip"
            dest = d / name
            url = f"{BASE}/{sym}/5m/{name}"
            want = _get(url + ".CHECKSUM").decode().split()[0]
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == want:
                got.append(dest)
                continue
            data = _get(url)
            if hashlib.sha256(data).hexdigest() != want:
                raise ValueError(f"checksum mismatch for {url}")
            dest.write_bytes(data)
            got.append(dest)
            print(f"{name}: {len(data) / 1e6:.2f} MB")
    return got


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--start", default="2022-01")
    ap.add_argument("--end", default="2026-10")
    a = ap.parse_args(argv)
    files = fetch(Path(a.out), a.start, a.end)
    print(f"{len(files)} files OK under {a.out}")


if __name__ == "__main__":
    main()
