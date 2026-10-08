"""Tardis.dev raw Binance frames -> our recorder's files, for ``research.replay``.

    uv run python -m research.collect.tardis_feed --start 2026-09-01 --end 2026-09-10
    OFI_RECORD_DIR=data/raw/tardis uv run python -m research.replay features \\
        --profile dev --date 2026-09-01

Tardis's raw API (``/v1/data-feeds``) serves the exact combined-stream frames
Binance sent its collector, one minute per request, as lines of
``<ISO receive time> <frame>``. Unlike its CSV exports they keep the depth
update ids (``U``/``u``/``pu``) the book sync verifies, so the frames are
written unchanged as recorder lines (``<recv_ns> <frame>``), in the recorder's
layout, and go through ``FuturesStreams.replay_line`` like our own recordings:

- streams match a diff-mode session: ``depth@0ms`` and ``bookTicker`` on the
  book side, ``aggTrade``, ``markPrice@1s`` and ``forceOrder``;
- Tardis's ``depthSnapshot`` frames are REST snapshots (one generated at each
  UTC day's start, more after its collector reconnects): replay applies them
  as it applies ours, buffering and bridging diffs by update id;
- each day gets a ``_control@session`` line just before its first frame
  (diff mode, tick sizes), so every day replays on its own.

Receive times are Tardis's collector's, not our Tokyo host's, so manifests
carry ``host: tardis`` and ``pipeline.eligible_days`` keeps these days out of
the binding gates. They are for exploring whether there is a signal at all.

A day is written into ``<dest>/.partial/`` and moved into place once complete,
so a rerun skips finished days and redoes an interrupted one.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import shutil
import sys
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx

from ofi_scalper_service.recorder import hour_path, symbol_and_stream

from .tardis import API_URL, EXCHANGE, api_key

__all__ = ["CHANNELS", "convert_day", "fetch_minute", "parse_iso_ns", "session_frame"]

HOST_TAG = "tardis"
CHANNELS = ("depth", "depthSnapshot", "bookTicker", "aggTrade", "markPrice", "forceOrder")
# Binance USDⓈ-M contract specs for the symbols we trade (exchangeInfo, 2026-10).
TICK_SIZES = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}
LOT_STEPS = {"BTCUSDT": 0.001, "ETHUSDT": 0.001}
MINUTES_PER_DAY = 1440
_RETRY_BACKOFF_SECONDS = (5, 15, 30, 60, 120)


def parse_iso_ns(stamp: str) -> int:
    """``2026-09-01T00:00:00.0259231Z`` -> epoch ns (Tardis gives 100 ns digits)."""
    whole, _, frac = stamp.rstrip("Z").partition(".")
    seconds = int(datetime.fromisoformat(whole).replace(tzinfo=UTC).timestamp())
    return seconds * 1_000_000_000 + int((frac + "000000000")[:9])


def session_frame(symbols: list[str]) -> str:
    lower = [s.lower() for s in symbols]
    data = {
        "symbols": symbols,
        "book_mode": "diff",
        "tick_sizes": {s: TICK_SIZES[s] for s in symbols},
        "lot_steps": {s: LOT_STEPS[s] for s in symbols},
        "streams": {
            "public": [n for s in lower for n in (f"{s}@depth@0ms", f"{s}@bookTicker")],
            "market": [
                n for s in lower for n in (f"{s}@aggTrade", f"{s}@markPrice@1s", f"{s}@forceOrder")
            ],
        },
        "source": HOST_TAG,
    }
    return json.dumps({"stream": "_control@session", "data": data}, separators=(",", ":"))


def fetch_minute(client: httpx.Client, day: date, offset: int, filters: str) -> list[str]:
    """One minute of raw lines, retrying server errors and dropped connections."""
    params = {"from": day.isoformat(), "offset": str(offset), "filters": filters}
    for attempt, backoff in enumerate((*_RETRY_BACKOFF_SECONDS, None), start=1):
        try:
            response = client.get(f"{API_URL}/data-feeds/{EXCHANGE}", params=params)
            if response.status_code < 500:
                response.raise_for_status()
                return [line for line in response.text.split("\n") if line]
            error: Exception = httpx.HTTPStatusError(
                f"{response.status_code}", request=response.request, response=response
            )
        except httpx.TransportError as exc:
            error = exc
        if backoff is None:
            raise RuntimeError(f"{day} minute {offset}: gave up after {attempt} tries") from error
        print(f"{day} minute {offset} retry {attempt} in {backoff}s: {error}", flush=True)
        time.sleep(backoff)
    raise AssertionError("unreachable")


@dataclass
class _Hour:
    path: Path
    hour: str
    handle: IO[str]
    lines: int = 0
    streams: Counter[str] = field(default_factory=Counter)
    first_ns: int = 0
    last_ns: int = 0


class _Writer:
    """The recorder's layout and manifests, written synchronously."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.open: dict[str, _Hour] = {}
        self.lines = 0

    def write(self, recv_ns: int, text: str) -> None:
        symbol, stream = symbol_and_stream(text)
        path, hour = hour_path(self.root, symbol, recv_ns)
        current = self.open.get(symbol)
        if current is not None and current.hour != hour:
            self.close(symbol)
            current = None
        if current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
            current = self.open[symbol] = _Hour(path, hour, handle, first_ns=recv_ns)
        current.handle.write(f"{recv_ns} {text}\n")
        current.lines += 1
        current.streams[stream] += 1
        current.last_ns = recv_ns
        self.lines += 1

    def close(self, symbol: str) -> None:
        current = self.open.pop(symbol)
        current.handle.close()
        body = {
            "symbol": symbol,
            "hour": current.hour,
            "host": HOST_TAG,
            "lines": current.lines,
            "streams": dict(current.streams),
            "first_recv_ns": current.first_ns,
            "last_recv_ns": current.last_ns,
            "gaps": 0,
            "sessions": 1,
        }
        manifest = current.path.with_name(current.path.name + ".json")
        manifest.write_text(json.dumps(body, indent=2))

    def close_all(self) -> None:
        for symbol in list(self.open):
            self.close(symbol)


def convert_day(
    client: httpx.Client,
    day: date,
    symbols: list[str],
    dest: Path,
    workers: int,
    minutes: int = MINUTES_PER_DAY,
) -> dict[str, Any]:
    """Fetch one UTC day and write it as recordings. Returns a summary."""
    stamp = day.strftime("%Y%m%d")
    partial = dest / ".partial" / stamp
    shutil.rmtree(partial, ignore_errors=True)
    lower = [s.lower() for s in symbols]
    filters = json.dumps([{"channel": c, "symbols": lower} for c in CHANNELS])
    writer = _Writer(partial)
    session_written = False
    last_ns = 0
    late = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: deque[Future[list[str]]] = deque()
        offsets = iter(range(minutes))
        for offset in offsets:
            pending.append(pool.submit(fetch_minute, client, day, offset, filters))
            if len(pending) >= workers * 2:
                break
        while pending:
            lines = pending.popleft().result()
            if (offset := next(offsets, None)) is not None:
                pending.append(pool.submit(fetch_minute, client, day, offset, filters))
            for line in lines:
                iso, _, frame = line.partition(" ")
                recv_ns = parse_iso_ns(iso)
                if not session_written:
                    writer.write(recv_ns - 1, session_frame(symbols))
                    session_written = True
                if recv_ns < last_ns:
                    late += 1  # keep the file monotonic, as the recorder's is
                    recv_ns = last_ns
                writer.write(recv_ns, frame)
                last_ns = recv_ns
    writer.close_all()
    for symbol_dir in partial.iterdir():
        target = dest / symbol_dir.name / stamp
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(symbol_dir / stamp), str(target))
    shutil.rmtree(partial)
    with contextlib.suppress(OSError):
        partial.parent.rmdir()  # only once no other day is in progress
    return {
        "date": day.isoformat(),
        "lines": writer.lines,
        "out_of_order": late,
        "seconds": round(time.monotonic() - started),
    }


def _done(dest: Path, day: date, symbols: list[str]) -> bool:
    stamp = day.strftime("%Y%m%d")
    return (
        all(len(list((dest / s / stamp).glob("*.gz"))) == 24 for s in symbols)
        and (dest / "_CONTROL" / stamp).exists()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tardis raw frames -> recordings")
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="inclusive")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--dest", type=Path, default=Path("data/raw/tardis"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--minutes", type=int, default=MINUTES_PER_DAY, help="smoke tests")
    args = parser.parse_args(argv)
    key = api_key(args.profile)
    if not key:
        print("no TARDIS_API_KEY", file=sys.stderr)
        return 1
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    headers = {"Authorization": f"Bearer {key}"}
    timeout = httpx.Timeout(60, read=180)
    limits = httpx.Limits(max_connections=args.workers)
    with httpx.Client(timeout=timeout, headers=headers, limits=limits) as client:
        day = args.start
        while day <= args.end:
            if _done(args.dest, day, symbols):
                print(json.dumps({"date": day.isoformat(), "skipped": "already converted"}))
            else:
                summary = convert_day(client, day, symbols, args.dest, args.workers, args.minutes)
                print(json.dumps(summary), flush=True)
            day += timedelta(days=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
