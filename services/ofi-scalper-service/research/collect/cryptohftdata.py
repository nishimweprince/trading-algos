"""CryptoHFTData hourly Parquet -> our recorder's files, for ``research.replay``.

    CRYPTOHFTDATA_API_KEY=... uv run python -m research.collect.cryptohftdata \\
        --start 2026-06-01 --end 2026-09-30 --jobs 6
    uv run python -m research.pipeline --profile dev --binding --host-tag cryptohftdata

CryptoHFTData (``api.cryptohftdata.com``) stores what its collector received
from Binance USDⓈ-M as one Parquet object per symbol, data type and UTC hour
(``binance_futures/<date>/<HH>/<SYMBOL>_<type>.parquet``), one row per book
level or trade with the collector's ``received_time`` in ns. This module
rebuilds the frames Binance sent and writes them as recorder lines
(``<recv_ns> <frame>``) in the recorder's layout, so they replay through
``FuturesStreams.replay_line`` like our own recordings:

- ``orderbook`` ``update`` rows, grouped by message (receive time and ``u``),
  become ``<s>@depth@0ms`` diffs with their ``U``/``u``/``pu``;
- the ``snapshot`` at each hour's start is the collector's verified book after
  the diff whose ``u`` is its ``last_update_id`` (the next diff's ``pu`` is that
  id), which is exactly our ``<s>@bookCheckpoint``. A snapshot that does not
  chain that way is written as a REST ``<s>@depthSnapshot`` and bridged by id;
- ``trades`` are per-fill ``@trade`` rows. Fills of one taker order at one
  price (consecutive ids, same trade time, price and side) are merged into one
  ``<s>@aggTrade``, as Binance aggregates them; insurance-fund and ADL fills,
  which aggTrade leaves out, are dropped;
- ``mark_price`` and ``liquidations`` become ``<s>@markPrice@1s`` and
  ``<s>@forceOrder``. There is no ``bookTicker`` (their ``ticker`` is the 24 h
  statistics stream); the features read the book, not bookTicker.

Each day gets a ``_control@session`` line (diff mode, tick sizes) at the day's
first nanosecond, and every market line of the day is kept after it, so every
day replays on its own. Receive times are CryptoHFTData's collector's (about
125 ms behind Binance's event time, against ~10-30 ms in Tokyo), so manifests
carry ``host: cryptohftdata``. ``pipeline.eligible_days`` keeps these days out
of a run unless ``--host-tag cryptohftdata`` asks for them, and such a run
says whether there is an edge at that latency, not whether Tokyo trading
passes.

A symbol-day is written into ``<dest>/.partial/`` and moved into place once
complete, so a rerun skips finished days and redoes an interrupted one. An
hour the API does not have (404) is skipped and listed in the summary; the
daily check then reports it missing and the pipeline excludes the day.
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from ofi_scalper_service.recorder import hour_path

__all__ = ["convert_symbol_day", "hour_frames", "main", "session_frame"]

API_URL = "https://api.cryptohftdata.com"
EXCHANGE = "binance_futures"
HOST_TAG = "cryptohftdata"
DATA_TYPES = ("orderbook", "trades", "mark_price", "liquidations")
# Binance USDⓈ-M contract specs for the symbols we trade (exchangeInfo, 2026-10).
TICK_SIZES = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}
LOT_STEPS = {"BTCUSDT": 0.001, "ETHUSDT": 0.001}
# aggTrade covers market trades only; the liquidation engine's fills print as
# MARKET (or carry no type in older rows).
SKIPPED_TRADE_TYPES = {"INSURANCE_FUND", "ADL"}
HOUR_NS = 3_600_000_000_000
_RETRY_BACKOFF_SECONDS = (2, 5, 15, 30, 60, 120)


def api_key(profile: str) -> str | None:
    """CRYPTOHFTDATA_API_KEY from the environment, else from ``.env.<profile>``."""
    if key := os.environ.get("CRYPTOHFTDATA_API_KEY", "").strip():
        return key
    env_file = Path(f".env.{profile}")
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "CRYPTOHFTDATA_API_KEY":
            return value.strip().strip("\"'") or None
    return None


def session_frame(symbols: list[str]) -> str:
    lower = [s.lower() for s in symbols]
    data = {
        "symbols": symbols,
        "book_mode": "diff",
        "tick_sizes": {s: TICK_SIZES[s] for s in symbols},
        "lot_steps": {s: LOT_STEPS[s] for s in symbols},
        "streams": {
            "public": [f"{s}@depth@0ms" for s in lower],
            "market": [
                n for s in lower for n in (f"{s}@aggTrade", f"{s}@markPrice@1s", f"{s}@forceOrder")
            ],
        },
        "source": HOST_TAG,
    }
    return json.dumps({"stream": "_control@session", "data": data}, separators=(",", ":"))


# --- download ---------------------------------------------------------------------


class Api:
    """Bearer-token downloads that wait out the rate limit. Thread-safe."""

    def __init__(self, key: str | None) -> None:
        self.key = key
        self.client = httpx.Client(
            timeout=httpx.Timeout(60, read=300), follow_redirects=True, http2=False
        )
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_at = 0.0

    def _bearer(self, refresh: bool = False) -> dict[str, str]:
        if self.key is None:
            return {}
        with self._lock:
            # Tokens last four hours; renew well before.
            if refresh or self._token is None or time.monotonic() - self._token_at > 3 * 3600:
                response = self.client.post(
                    f"{API_URL}/v1/jwt-token", headers={"X-API-Key": self.key}
                )
                response.raise_for_status()
                body = response.json()
                self._token = body["jwt_token"]
                self._token_at = time.monotonic()
            return {"Authorization": f"Bearer {self._token}"}

    def download(self, file: str, dest: Path) -> bool:
        """``file`` into ``dest``; False when the API has no such object."""
        refreshed = False
        for attempt, backoff in enumerate((*_RETRY_BACKOFF_SECONDS, None), start=1):
            try:
                with self.client.stream(
                    "GET", f"{API_URL}/v1/download", params={"file": file}, headers=self._bearer()
                ) as response:
                    if response.status_code == 404:
                        return False
                    if response.status_code == 401 and not refreshed:
                        refreshed = True
                        self._bearer(refresh=True)
                        continue
                    if response.status_code == 429:
                        wait = float(
                            response.headers.get("retry-after")
                            or response.headers.get("ratelimit-reset")
                            or response.headers.get("x-ratelimit-reset")
                            or 10
                        )
                        time.sleep(min(max(wait, 1.0), 120.0))
                        continue
                    if response.status_code < 500:
                        response.raise_for_status()
                        tmp = dest.with_name(dest.name + ".part")
                        with tmp.open("wb") as out:
                            for chunk in response.iter_bytes(1 << 20):
                                out.write(chunk)
                        tmp.replace(dest)
                        return True
                    error: Exception = httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
            except httpx.TransportError as exc:
                error = exc
            if backoff is None:
                raise RuntimeError(f"{file}: gave up after {attempt} tries") from error
            print(f"{file} retry {attempt} in {backoff}s: {error}", flush=True)
            time.sleep(backoff)
        raise AssertionError("unreachable")


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
PARQUET_MAGIC = b"PAR1"


def plain_parquet(path: Path) -> Path:
    """``path`` as a readable Parquet file, decompressing a zstd download in place.

    Some objects (mostly hours 00–01) arrive with ``Content-Encoding: zstd``,
    which httpx does not decode without the zstandard package, so the body is
    the compressed file. pyarrow's own zstd codec undoes it. A file that is
    still not Parquet is deleted, so a rerun downloads it again.
    """
    with path.open("rb") as handle:
        head = handle.read(4)
    if head == ZSTD_MAGIC:
        tmp = path.with_name(path.name + ".unzstd")
        with pa.input_stream(str(path), compression="zstd") as source, tmp.open("wb") as out:
            while chunk := source.read(1 << 22):
                out.write(chunk)
        tmp.replace(path)
    with path.open("rb") as handle:
        head = handle.read(4)
        handle.seek(-4, os.SEEK_END)
        tail = handle.read(4)
    if head != PARQUET_MAGIC or tail != PARQUET_MAGIC:
        path.unlink(missing_ok=True)
        raise ValueError(
            f"{path}: not a Parquet file after download (head {head!r}); deleted for a retry"
        )
    return path


def object_name(day: date, hour: int, symbol: str, data_type: str) -> str:
    return f"{EXCHANGE}/{day.isoformat()}/{hour:02d}/{symbol}_{data_type}.parquet"


# --- rows -> frames ---------------------------------------------------------------


def _dump(stream: str, data: dict[str, Any]) -> str:
    return json.dumps({"stream": stream, "data": data}, separators=(",", ":"))


def _columns(path: Path | None, names: list[str]) -> dict[str, list[Any]] | None:
    if path is None or not path.exists():
        return None
    table = pq.read_table(path, columns=names)
    return {name: table.column(name).to_pylist() for name in names}


BOOK_BATCH_ROWS = 200_000


def book_frames(symbol: str, path: Path | None, stats: Counter[str]) -> Iterator[tuple[int, str]]:
    """Depth diffs, checkpoints and snapshots of one hour, in file order.

    Streamed in row batches: a busy hour holds 15M+ level rows, which as
    Python lists took one process past 8 GB.
    """
    names = [
        "received_time",
        "event_time",
        "transaction_time",
        "event_type",
        "first_update_id",
        "final_update_id",
        "prev_final_update_id",
        "last_update_id",
        "side",
        "price",
        "quantity",
    ]
    if path is None or not path.exists():
        return
    lower = symbol.lower()
    # A snapshot is typed once the next diff shows whether it chains.
    held: tuple[int, dict[str, Any]] | None = None

    def snapshot(recv_ns: int, data: dict[str, Any], chains: bool) -> tuple[int, str]:
        stream = "bookCheckpoint" if chains else "depthSnapshot"
        stats[stream] += 1
        return recv_ns, _dump(f"{lower}@{stream}", data)

    def finish(message: dict[str, Any]) -> Iterator[tuple[int, str]]:
        nonlocal held
        recv_ns = message.pop("_recv")
        if message.pop("_kind") == "update":
            if held is not None:
                yield snapshot(*held, chains=message["pu"] == held[1]["lastUpdateId"])
                held = None
            stats["depth"] += 1
            yield recv_ns, _dump(f"{lower}@depth@0ms", message)
            return
        if held is not None:
            yield snapshot(*held, chains=False)
        held = (recv_ns, message)

    message: dict[str, Any] | None = None
    key: tuple[Any, ...] | None = None
    for batch in pq.ParquetFile(path).iter_batches(batch_size=BOOK_BATCH_ROWS, columns=names):
        cols = {name: batch.column(name).to_pylist() for name in names}
        rows = zip(*(cols[name] for name in names), strict=True)
        for recv_ns, event_ms, txn_ms, kind, first, final, prev, last, side, px, q in rows:
            if (recv_ns, final, kind) != key:
                if message is not None:
                    yield from finish(message)
                key = (recv_ns, final, kind)
                if kind == "update":
                    message = {
                        "_recv": recv_ns,
                        "_kind": kind,
                        "e": "depthUpdate",
                        "E": event_ms,
                        "T": txn_ms,
                        "s": symbol,
                        "U": first,
                        "u": final,
                        "pu": prev,
                        "b": [],
                        "a": [],
                    }
                else:
                    message = {
                        "_recv": recv_ns,
                        "_kind": kind,
                        "lastUpdateId": last or final,
                        "bids": [],
                        "asks": [],
                    }
            assert message is not None
            if kind == "update":
                message["b" if side == "bid" else "a"].append([px, q])
            else:
                message["bids" if side == "bid" else "asks"].append([px, q])
    if message is not None:
        yield from finish(message)
    if held is not None:
        yield snapshot(*held, chains=False)


def trade_frames(symbol: str, path: Path | None) -> tuple[list[tuple[int, str]], Counter[str]]:
    """Per-fill trades merged into aggTrades (one taker order at one price)."""
    names = [
        "received_time",
        "event_time",
        "trade_id",
        "price",
        "quantity",
        "trade_time",
        "is_buyer_maker",
        "order_type",
    ]
    cols = _columns(path, names)
    stats: Counter[str] = Counter()
    if cols is None:
        return [], stats
    lower = symbol.lower()
    out: list[tuple[int, str]] = []
    current: dict[str, Any] | None = None
    current_recv = 0
    qty_sum = 0.0

    def flush() -> None:
        if current is not None:
            current["q"] = f"{qty_sum:.8f}".rstrip("0").rstrip(".")
            out.append((current_recv, _dump(f"{lower}@aggTrade", current)))
            stats["aggTrade"] += 1

    rows = zip(*(cols[name] for name in names), strict=True)
    for recv_ns, event_ms, trade_id, px, q, trade_ms, maker, order_type in rows:
        if order_type in SKIPPED_TRADE_TYPES:
            stats["skipped_fills"] += 1
            continue
        stats["fills"] += 1
        if (
            current is not None
            and trade_id == current["l"] + 1
            and trade_ms == current["T"]
            and px == current["p"]
            and maker == current["m"]
        ):
            current["l"] = trade_id
            qty_sum += float(q)
            continue
        flush()
        current = {
            "e": "aggTrade",
            "E": event_ms,
            "a": trade_id,
            "s": symbol,
            "p": px,
            "q": q,
            "f": trade_id,
            "l": trade_id,
            "T": trade_ms,
            "m": bool(maker),
        }
        current_recv = recv_ns
        qty_sum = float(q)
    flush()
    return out, stats


def mark_frames(symbol: str, path: Path | None) -> list[tuple[int, str]]:
    names = [
        "received_time",
        "event_time",
        "mark_price",
        "index_price",
        "estimated_settle_price",
        "funding_rate",
        "next_funding_time",
    ]
    cols = _columns(path, names)
    if cols is None:
        return []
    lower = symbol.lower()
    rows = zip(*(cols[name] for name in names), strict=True)
    return [
        (
            recv_ns,
            _dump(
                f"{lower}@markPrice@1s",
                {
                    "e": "markPriceUpdate",
                    "E": e,
                    "s": symbol,
                    "p": p,
                    "i": i,
                    "P": s,
                    "r": r,
                    "T": t,
                },
            ),
        )
        for recv_ns, e, p, i, s, r, t in rows
    ]


def liquidation_frames(symbol: str, path: Path | None) -> list[tuple[int, str]]:
    names = [
        "received_time",
        "event_time",
        "side",
        "order_type",
        "time_in_force",
        "quantity",
        "price",
        "average_price",
        "order_status",
        "last_filled_quantity",
        "filled_quantity",
        "trade_time",
    ]
    cols = _columns(path, names)
    if cols is None:
        return []
    lower = symbol.lower()
    rows = zip(*(cols[name] for name in names), strict=True)
    out = []
    for recv_ns, e, side, otype, tif, q, p, ap, status, last, filled, trade_ms in rows:
        order = {
            "s": symbol,
            "S": side,
            "o": otype,
            "f": tif,
            "q": q,
            "p": p,
            "ap": ap,
            "X": status,
            "l": last,
            "z": filled,
            "T": trade_ms,
        }
        out.append((recv_ns, _dump(f"{lower}@forceOrder", {"e": "forceOrder", "E": e, "o": order})))
    return out


def hour_frames(
    symbol: str, files: dict[str, Path | None], stats: Counter[str]
) -> Iterator[tuple[int, str]]:
    """Every frame of one symbol-hour, ordered by receive time (book first on ties).

    The book streams; trades, marks and liquidations are small enough to hold.
    ``stats`` fills in as the frames are consumed.
    """
    trades, trade_stats = trade_frames(symbol, files.get("trades"))
    stats.update(trade_stats)
    marks = mark_frames(symbol, files.get("mark_price"))
    liquidations = liquidation_frames(symbol, files.get("liquidations"))
    stats["markPrice"] += len(marks)
    stats["forceOrder"] += len(liquidations)
    rest = sorted(trades + marks + liquidations, key=lambda row: row[0])
    book = book_frames(symbol, files.get("orderbook"), stats)
    return heapq.merge(book, rest, key=lambda row: row[0])


# --- writing ----------------------------------------------------------------------


# ``recordings.recorded_lines`` hands lines out only at a gzip sync point (or a
# member's end). Without them a whole hour (~250 MB of text) is held before its
# first line, and a day replay opens 49 files at once (~9 GB).
FLUSH_LINES = 20_000


def resync(path: Path) -> None:
    """Rewrite a file written without sync points, adding one every FLUSH_LINES."""
    tmp = path.with_name(path.name + ".resync")
    with (
        gzip.open(path, "rt", encoding="utf-8") as source,
        gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as out,
    ):
        for index, line in enumerate(source):
            out.write(line)
            if index % FLUSH_LINES == FLUSH_LINES - 1:
                out.flush()
    tmp.replace(path)
    manifest = path.with_name(path.name + ".json")
    if manifest.exists():
        body = json.loads(manifest.read_text())
        body["sync_points"] = True
        manifest.write_text(json.dumps(body, indent=2))


def _needs_resync(path: Path) -> bool:
    manifest = path.with_name(path.name + ".json")
    return manifest.exists() and not json.loads(manifest.read_text()).get("sync_points")


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
    """The recorder's layout and manifests, for one symbol (or _CONTROL)."""

    def __init__(self, root: Path, symbol: str) -> None:
        self.root = root
        self.symbol = symbol
        self.current: _Hour | None = None
        self.lines = 0
        self.last_ns = 0
        self.clamped = 0

    def write(self, recv_ns: int, text: str, stream: str) -> None:
        if recv_ns < self.last_ns:
            self.clamped += 1  # keep the file monotonic, as the recorder's is
            recv_ns = self.last_ns
        path, hour = hour_path(self.root, self.symbol, recv_ns)
        if self.current is not None and self.current.hour != hour:
            self.close()
        if self.current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
            self.current = _Hour(path, hour, handle, first_ns=recv_ns)
        self.current.handle.write(f"{recv_ns} {text}\n")
        if self.current.lines % FLUSH_LINES == FLUSH_LINES - 1:
            self.current.handle.flush()  # a sync point, as the recorder's periodic flush
        self.current.lines += 1
        self.current.streams[stream] += 1
        self.current.last_ns = recv_ns
        self.last_ns = recv_ns
        self.lines += 1

    def close(self) -> None:
        current, self.current = self.current, None
        if current is None:
            return
        current.handle.close()
        body = {
            "symbol": self.symbol,
            "hour": current.hour,
            "host": HOST_TAG,
            "lines": current.lines,
            "streams": dict(current.streams),
            "first_recv_ns": current.first_ns,
            "last_recv_ns": current.last_ns,
            "gaps": 0,
            "sessions": 1,
            "sync_points": True,
        }
        manifest = current.path.with_name(current.path.name + ".json")
        manifest.write_text(json.dumps(body, indent=2))


def _stream_of(text: str) -> str:
    start = text.find('"stream":"') + 10
    name = text[start : text.find('"', start)]
    return name.partition("@")[2]


def write_session(dest: Path, day: date, symbols: list[str]) -> None:
    stamp = day.strftime("%Y%m%d")
    target = dest / "_CONTROL" / stamp
    if target.exists():
        return
    partial = dest / ".partial" / f"{stamp}-_CONTROL"
    shutil.rmtree(partial, ignore_errors=True)
    writer = _Writer(partial, "_CONTROL")
    start_ns = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 10**9
    writer.write(start_ns, session_frame(symbols), "session")
    writer.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(partial / "_CONTROL" / stamp), str(target))
    shutil.rmtree(partial)


def convert_symbol_day(
    day: date, symbol: str, dest: Path, key: str | None, keep_parquet: Path | None = None
) -> dict[str, Any]:
    """Download and write one symbol-day. Returns a summary."""
    started = time.monotonic()
    stamp = day.strftime("%Y%m%d")
    partial = dest / ".partial" / f"{stamp}-{symbol}"
    shutil.rmtree(partial, ignore_errors=True)
    day_ns = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 10**9
    api = Api(key)
    writer = _Writer(partial, symbol)
    stats: Counter[str] = Counter()
    missing: list[str] = []
    bytes_in = 0
    # Not /tmp: it is RAM-backed (tmpfs) on Amazon Linux 2023.
    (dest / ".tmp").mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix=f"{stamp}-{symbol}-", dir=dest / ".tmp") as tmp,
        ThreadPoolExecutor(max_workers=len(DATA_TYPES) * 2) as pool,
    ):
        tmp_dir = Path(tmp)

        def fetch(hour: int) -> dict[str, Any]:
            jobs = {}
            for data_type in DATA_TYPES:
                name = object_name(day, hour, symbol, data_type)
                local = (keep_parquet / name) if keep_parquet else tmp_dir / name.replace("/", "_")
                local.parent.mkdir(parents=True, exist_ok=True)
                if local.exists():
                    jobs[data_type] = None
                    continue
                jobs[data_type] = pool.submit(api.download, name, local)
            files: dict[str, Path | None] = {}
            for data_type, job in jobs.items():
                name = object_name(day, hour, symbol, data_type)
                local = (keep_parquet / name) if keep_parquet else tmp_dir / name.replace("/", "_")
                found = True if job is None else job.result()
                files[data_type] = plain_parquet(local) if found else None
            return files

        # Prefetch one hour ahead while the current one converts.
        ahead = ThreadPoolExecutor(max_workers=1)
        pending = ahead.submit(fetch, 0)
        for hour in range(24):
            files = pending.result()
            if hour < 23:
                pending = ahead.submit(fetch, hour + 1)
            for data_type, path in files.items():
                if path is None:
                    if data_type in ("orderbook", "trades"):
                        missing.append(f"{hour:02d}:{data_type}")
                else:
                    bytes_in += path.stat().st_size
            if files.get("orderbook") is None:
                stats["hours_without_book"] += 1
            else:
                for recv_ns, text in hour_frames(symbol, files, stats):
                    writer.write(max(recv_ns, day_ns + 1), text, _stream_of(text))
            if keep_parquet is None:
                for path in files.values():
                    if path is not None:
                        path.unlink(missing_ok=True)
        ahead.shutdown()
    writer.close()
    api.client.close()
    if writer.lines:
        target = dest / symbol / stamp
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(partial / symbol / stamp), str(target))
    shutil.rmtree(partial, ignore_errors=True)
    return {
        "date": day.isoformat(),
        "symbol": symbol,
        "lines": writer.lines,
        "clamped": writer.clamped,
        "missing": missing,
        "mb_in": round(bytes_in / 1e6),
        "seconds": round(time.monotonic() - started),
        **dict(stats),
    }


def _done(dest: Path, day: date, symbol: str) -> bool:
    stamp = day.strftime("%Y%m%d")
    return len(list((dest / symbol / stamp).glob("*.gz"))) == 24


def _job(args: tuple[date, str, Path, str | None, Path | None]) -> dict[str, Any]:
    return convert_symbol_day(*args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoHFTData Parquet -> recordings")
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="inclusive")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--dest", type=Path, default=Path("data/raw/cryptohftdata"))
    parser.add_argument("--jobs", type=int, default=4, help="symbol-days converted at once")
    parser.add_argument("--newest-first", action="store_true", help="convert the latest days first")
    parser.add_argument(
        "--keep-parquet", type=Path, default=None, help="keep the downloads under this directory"
    )
    parser.add_argument(
        "--min-free-gb", type=float, default=40, help="stop submitting below this much free disk"
    )
    parser.add_argument(
        "--resync",
        action="store_true",
        help="only add sync points to files converted before they were written",
    )
    args = parser.parse_args(argv)
    if args.resync:
        old = [p for p in sorted(args.dest.glob("*/*/*.gz")) if _needs_resync(p)]
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for path, _ in zip(old, pool.map(resync, old), strict=True):
                print(json.dumps({"resynced": str(path)}), flush=True)
        return 0
    key = api_key(args.profile)
    if not key:
        print("no CRYPTOHFTDATA_API_KEY: using the anonymous rate limit", file=sys.stderr)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    args.dest.mkdir(parents=True, exist_ok=True)
    days = [args.start + timedelta(days=n) for n in range((args.end - args.start).days + 1)]
    if args.newest_first:
        days.reverse()
    todo = []
    for day in days:
        write_session(args.dest, day, symbols)
        for symbol in symbols:
            if _done(args.dest, day, symbol):
                print(json.dumps({"date": day.isoformat(), "symbol": symbol, "skipped": "done"}))
            else:
                todo.append((day, symbol, args.dest, key, args.keep_parquet))
    started = datetime.now(UTC).isoformat(timespec="seconds")
    print(json.dumps({"run_started": started, "todo": len(todo)}), flush=True)
    failures = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        queue = iter(todo)
        running = {}
        for job in queue:
            running[pool.submit(_job, job)] = job
            if len(running) >= args.jobs:
                break
        while running:
            future = next(as_completed(running))
            job = running.pop(future)
            try:
                print(json.dumps(future.result()), flush=True)
            except Exception as exc:  # one bad day must not stop the rest
                failures += 1
                print(
                    json.dumps({"date": job[0].isoformat(), "symbol": job[1], "error": repr(exc)}),
                    flush=True,
                )
            free_gb = shutil.disk_usage(args.dest).free / 1e9
            if free_gb < args.min_free_gb:
                print(json.dumps({"stopped": "disk", "free_gb": round(free_gb, 1)}), flush=True)
                continue
            if (nxt := next(queue, None)) is not None:
                running[pool.submit(_job, nxt)] = nxt
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
