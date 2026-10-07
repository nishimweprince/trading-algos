"""Streaming reader for the Helix kpc tape (``.hlxz``). Import this, never ``recv3``.

Two paths over the same bytes:

- **Typed records** (``iter_*``): one dataclass per record, through ``recv3``
  unchanged. For small streams (windows, resolutions, trades) and for checks.
- **Series** (``*_series``): one zstd block at a time into ``numpy.frombuffer``,
  filtered to the wanted tokens or assets before anything is kept. A Polymarket
  BBO day is ~262M records per side (all 14 up/down markets, 5m and 15m), which
  the typed path decodes at about 0.5M records/s; this path is what
  ``research.helix_replay`` uses. ``validate.py`` checks it field for field
  against ``recv3`` on a sample.

Nothing here writes a decoded copy of the tape (PLAN 0.1): every function
yields or returns in memory, and callers keep only derived outputs.

Clocks (README.md has the measurements):

- ``recv_ns``: the recorder's receive time, CLOCK_MONOTONIC on the Helix host,
  for every BBO stream and for Polymarket trades. Orders records within the
  host; it is not a calendar time.
- ``wall_ns``: CLOCK_REALTIME at receive, on the same host. Every array path
  returns this as ``t`` (wall ns), so windows (``open_unix``) and our own tape
  (``recv_ns`` is wall) line up with it.
- ``venue_ns``: the venue's own stamp, where there is one (Polymarket BBO's is
  mapped into the monotonic plane by the recorder; Polymarket and Kalshi trades
  carry wall-clock venue time).
"""

from __future__ import annotations

import mmap
import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zstandard as zstd

from . import recv3

__all__ = [
    "ASSETS",
    "HELIX_15M",
    "STREAMS",
    "PolyResolution",
    "days",
    "iter_chainlink",
    "iter_kalshi_quotes",
    "iter_kalshi_trades",
    "iter_poly_bbo",
    "iter_poly_trades",
    "iter_resolutions",
    "iter_windows",
    "Series",
    "chainlink_prices",
    "kalshi_quote_series",
    "path",
    "poly_bbo_series",
    "tape_dir",
]

# Helix asset id -> our asset name, 15-minute markets only. The tape also holds
# the 5-minute ones (even ids); nothing here reads them.
HELIX_15M = {1: "BTC", 3: "ETH", 5: "SOL", 7: "XRP", 9: "BNB", 11: "DOGE", 13: "HYPE"}
ASSETS = {name: aid for aid, name in HELIX_15M.items()}
# Chainlink reference prices are keyed by the *Spot slot.
CHAINLINK_SPOT = {
    "BTC": 15, "ETH": 16, "SOL": 17, "XRP": 18, "BNB": 19, "DOGE": 20, "HYPE": 21,
}  # fmt: skip

STREAMS = (
    "Polymarket_Window",
    "Polymarket_CLOB_Yes_BBO",
    "Polymarket_CLOB_No_BBO",
    "Polymarket_CLOB_Trade",
    "Polymarket_CLOB_No_Trade",
    "PolymarketChainlink_BBO",
    "Kalshi_Window",
    "Kalshi_Yes_BBO",
    "Kalshi_No_BBO",
    "Kalshi_Yes_Trade",
    "Kalshi_No_Trade",
)


def tape_dir(root: str | Path | None = None) -> Path:
    """``root``, else ``$HELIX_TAPE_DIR``, else ``$KPC_DATA``, else ``~/kpc/raw``."""
    if root is not None:
        return Path(root).expanduser()
    env = os.environ.get("HELIX_TAPE_DIR") or os.environ.get("KPC_DATA")
    return Path(env).expanduser() if env else Path.home() / "kpc" / "raw"


def path(stream: str, day: str, root: str | Path | None = None) -> Path:
    return tape_dir(root) / stream / f"{day}.hlxz"


def days(stream: str, root: str | Path | None = None) -> list[str]:
    return sorted(p.stem for p in (tape_dir(root) / stream).glob("*.hlxz"))


# --- typed records ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolyResolution:
    """Tag 33 (ns) / 47 (wide) / 8 (legacy, us), 96 B; not decoded by ``recv3``.

    Layout, from the bytes (README.md): ``<QQ 64s d B B H 4x>`` = recorded ts,
    window open, condition id (63 hex chars, no ``0x``), close price, a constant
    0xFF byte, asset, asset_wide. ``close_px`` equals the next window's strike
    in every joinable window checked: Helix derives it from strikes, so it is
    not Polymarket's own resolution message.
    """

    ts_recorded_ns: int
    open_unix: int
    condition_id: str
    close_px: float
    asset: int


_RES = struct.Struct("<QQ64sdBBH4x")
assert _RES.size == 96
_TAG_RES = {33: 1, 47: 1, 8: 1000}


def iter_resolutions(day: str, root: str | Path | None = None) -> Iterator[PolyResolution]:
    p = str(path("Polymarket_Window", day, root))
    for tag, scale in _TAG_RES.items():
        for rec in recv3.iter_records(p, tag=tag, rsz=96):
            ts, open_unix, cond, close_px, _ff, aid, wide = _RES.unpack(rec)
            yield PolyResolution(
                ts * scale, open_unix, cond.split(b"\0", 1)[0].decode(), close_px, wide or aid
            )


def iter_windows(day: str, root: str | Path | None = None) -> Iterator[recv3.PolyWindow]:
    """Polymarket 15-minute windows (strike, YES/NO tokens), once each.

    5-minute windows are dropped. A recorder restart re-emits the windows it
    knows (07-26: 707 records for 672 windows, the repeats identical), so the
    first record per (asset, open) is kept; a repeat with other tokens raises.
    """
    seen: dict[tuple[int, int], tuple[str, str]] = {}
    for w in recv3.iter_poly_windows(str(path("Polymarket_Window", day, root))):
        if w.asset not in HELIX_15M:
            continue
        key, ids = (w.asset, w.open_unix), (w.token_yes, w.token_no)
        if key in seen:
            if seen[key] != ids:
                raise ValueError(f"{day}: window {key} recorded with two token pairs")
            continue
        seen[key] = ids
        yield w


def iter_poly_bbo(stream: str, day: str, root: str | Path | None = None):
    return recv3.iter_poly_bbo(str(path(stream, day, root)))


def iter_kalshi_quotes(stream: str, day: str, root: str | Path | None = None):
    return recv3.iter_quotes(str(path(stream, day, root)), only_changed=True)


def iter_chainlink(day: str, root: str | Path | None = None):
    return recv3.iter_quotes(str(path("PolymarketChainlink_BBO", day, root)), only_changed=False)


def iter_poly_trades(stream: str, day: str, root: str | Path | None = None):
    return recv3.iter_poly_trades(str(path(stream, day, root)))


def iter_kalshi_trades(stream: str, day: str, root: str | Path | None = None):
    return recv3.iter_trades(str(path(stream, day, root)))


# --- blocks -> arrays ------------------------------------------------------------


def iter_blocks(p: str | Path, tags: dict[int, int], rsz: int) -> Iterator[tuple[bytes, int]]:
    """``(decompressed block, time scale)`` for each block whose tag is in ``tags``.

    The same walk as ``recv3.iter_records`` (FileHeader, inline block walk that
    resyncs across the sub-files a restart appends, footer only for a single
    sub-file, stale-width blocks skipped), minus the per-record slicing.
    """
    with open(p, "rb") as fh:
        try:
            data: mmap.mmap | bytes = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            data = fh.read()
    if len(data) < 32:
        return
    magic, ver, *_ = recv3._FILE_HDR.unpack_from(data, 0)
    if magic != recv3._FILE_MAGIC or ver != 1:
        raise ValueError(f"{p}: not a v1 .hlxz container")
    file_magic = struct.pack("<I", recv3._FILE_MAGIC)
    dctx = zstd.ZstdDecompressor()
    off, n = 32, len(data)
    while off + 40 <= n:
        if struct.unpack_from("<I", data, off)[0] == recv3._FILE_MAGIC:
            off += 32
            continue
        bm, tag, _p, brsz, count, comp, uncomp, _f, _l = recv3._BLK_HDR.unpack_from(data, off)
        if bm != recv3._BLK_MAGIC:
            nxt = data.find(file_magic, off + 4)
            if nxt < 0:
                break
            off = nxt
            continue
        body = off + 40
        if body + comp > n:
            break  # partial tail
        off = body + comp
        if tag not in tags or brsz != rsz:
            continue
        try:
            raw = dctx.decompress(data[body : body + comp], max_output_size=uncomp)
        except zstd.ZstdError:
            continue  # recv3 skips an undecodable block the same way
        yield raw[: count * rsz], tags[tag]


def _tags(canonical: int) -> dict[int, int]:
    legacy, scale = recv3._DUAL_TAG[canonical]
    tags = {canonical: 1, legacy: scale}
    wide = recv3._WIDE_TAG.get(canonical)
    if wide is not None:
        tags[wide] = 1
    return tags


_POLY_BBO_DT = np.dtype(
    {
        "names": ["recv", "wall", "pub", "bid", "bsz", "ask", "asz", "k0", "k1", "k2"],
        "formats": ["<i8"] * 7 + ["<u8"] * 3,
        "offsets": [0, 8, 16, 24, 32, 40, 48, 136, 144, 152],
        "itemsize": 224,
    }
)
_QUOTE_DT = np.dtype(
    {
        "names": ["bid", "ask", "bsz", "asz", "recv", "wall", "seq", "aid", "chg"],
        "formats": ["<f8"] * 4 + ["<i8", "<i8", "<u8", "u1", "u1"],
        "offsets": [0, 8, 16, 24, 32, 40, 48, 58, 59],
        "itemsize": 64,
    }
)
PX = 10_000  # prices are kept as int16 ticks of 1e-4: exact, and 0.60 compares as 6000


@dataclass
class Series:
    """Top-of-book rows grouped by key, each group in time order.

    Columns: ``t`` (wall ns, int64), ``bid``/``ask`` (int16, price x ``PX``; 0 is
    an empty side), ``bsz``/``asz`` (float32 shares at the touch), plus whatever
    ``extra`` the caller asked for. ``rows(k)`` is the slice of key ``k``.
    """

    cols: dict[str, np.ndarray]
    starts: np.ndarray
    ends: np.ndarray
    raw_rows: int  # matched rows before dedup

    def rows(self, key: int) -> slice:
        return slice(int(self.starts[key]), int(self.ends[key]))

    def __len__(self) -> int:
        return len(self.cols["t"])


def _token_key(token: str) -> tuple[int, int, int]:
    raw = token.encode().ljust(24, b"\0")[:24]
    return struct.unpack("<QQQ", raw)


def _mix(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """One 64-bit key from the token's first 24 bytes (matches are then checked in full)."""
    with np.errstate(over="ignore"):
        return a ^ (b * np.uint64(0x9E3779B97F4A7C15)) ^ (c * np.uint64(0xC2B2AE3D27D4EB4F))


def _px(values: np.ndarray, scale: float) -> np.ndarray:
    return np.rint(values * (PX / scale)).astype(np.int16)


def poly_bbo_series(
    stream: str,
    day: str,
    tokens: list[str],
    root: str | Path | None = None,
    *,
    dedup: bool = True,
    extra: tuple[str, ...] = (),
) -> Series:
    """Polymarket BBO for ``tokens`` only; key ``k`` is ``tokens[k]``.

    Tokens are matched on the first 24 bytes of the decimal id (a sorted lookup
    on a 64-bit mix of them, then all 24 compared), so a block costs
    microseconds, not a Python loop. ``dedup``: see ``_finish``. ``extra`` may name ``recv``
    (monotonic receive ns) and ``pub`` (venue ns, same plane).
    """
    if len(set(tokens)) != len(tokens):
        raise ValueError("tokens must be distinct")
    keys = np.array([_token_key(t) for t in tokens], dtype=np.uint64).reshape(-1, 3)
    mixed = _mix(keys[:, 0], keys[:, 1], keys[:, 2])
    order = np.argsort(mixed, kind="stable")
    k0 = mixed[order]
    if len(k0) and (np.diff(k0) == 0).any():
        raise ValueError("two tokens share a 64-bit key; widen the lookup")
    k12 = keys[order]
    idx = order.astype(np.int16)
    parts: list[dict[str, np.ndarray]] = []
    raw_rows = 0
    for raw, scale in iter_blocks(path(stream, day, root), _tags(recv3.TAG_POLY_BBO_NS), 224):
        a = np.frombuffer(raw, dtype=_POLY_BBO_DT)
        if not len(k0):
            break
        m = _mix(a["k0"], a["k1"], a["k2"])
        pos = np.minimum(np.searchsorted(k0, m), len(k0) - 1)
        hit = (k0[pos] == m) & (k12[pos, 0] == a["k0"]) & (k12[pos, 1] == a["k1"])
        hit &= k12[pos, 2] == a["k2"]
        if not hit.any():
            continue
        a, key = a[hit], idx[pos[hit]]
        raw_rows += len(a)
        part = {
            "t": a["wall"] * scale,
            "key": key,
            "bid": _px(a["bid"], 1e8),
            "ask": _px(a["ask"], 1e8),
            "bsz": (a["bsz"] / 1e8).astype(np.float32),
            "asz": (a["asz"] / 1e8).astype(np.float32),
        }
        for name in extra:
            part[name] = a[name] * scale
        parts.append(_dedup(part) if dedup else part)
    return _finish(parts, len(tokens), raw_rows, dedup)


def kalshi_quote_series(
    stream: str,
    day: str,
    asset_ids: list[int],
    root: str | Path | None = None,
    *,
    dedup: bool = True,
    extra: tuple[str, ...] = (),
) -> Series:
    """Kalshi BBO (SpotQuote records) for ``asset_ids``; key ``k`` is ``asset_ids[k]``.

    Only ``changed`` records, as ``recv3.iter_quotes`` reads by default: the
    others are the recorder's stale re-stamps. ``extra`` may name ``recv``
    and ``seq``.
    """
    lut = np.full(256, -1, dtype=np.int16)
    for k, aid in enumerate(asset_ids):
        lut[aid] = k
    parts: list[dict[str, np.ndarray]] = []
    raw_rows = 0
    for raw, scale in iter_blocks(path(stream, day, root), _tags(recv3.TAG_QUOTE_NS), 64):
        a = np.frombuffer(raw, dtype=_QUOTE_DT)
        key = lut[a["aid"]]
        keep = (key >= 0) & (a["chg"] == 1)
        if not keep.any():
            continue
        a, key = a[keep], key[keep]
        raw_rows += len(a)
        part = {
            "t": a["wall"] * scale,
            "key": key,
            "bid": _px(a["bid"], 1.0),
            "ask": _px(a["ask"], 1.0),
            "bsz": a["bsz"].astype(np.float32),
            "asz": a["asz"].astype(np.float32),
        }
        for name in extra:
            part[name] = a[name] * (scale if name == "recv" else 1)
        parts.append(_dedup(part) if dedup else part)
    return _finish(parts, len(asset_ids), raw_rows, dedup)


def chainlink_prices(
    day: str, asset_ids: list[int], root: str | Path | None = None
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Chainlink reference price per *Spot asset id: ``(t wall ns, price)``, time order."""
    out: dict[int, list[np.ndarray]] = {a: [] for a in asset_ids}
    p = path("PolymarketChainlink_BBO", day, root)
    if not p.exists():
        return {}
    for raw, scale in iter_blocks(p, _tags(recv3.TAG_QUOTE_NS), 64):
        a = np.frombuffer(raw, dtype=_QUOTE_DT)
        for aid in asset_ids:
            m = a["aid"] == aid
            if m.any():
                out[aid].append(np.stack([a["wall"][m] * scale, a["bid"][m].view(np.int64)]))
    result = {}
    for aid, chunks in out.items():
        if not chunks:
            continue
        both = np.concatenate(chunks, axis=1)
        o = np.argsort(both[0], kind="stable")
        result[aid] = (both[0][o], both[1][o].view(np.float64))
    return result


def _dedup(cols: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Group by key in time order and drop rows whose touch repeats the previous
    row of their key, except the first row of each 900 s grid window: "updated
    since the window opened" (the live ghost-gap guard) must still see it.

    Run per block to bound memory, then once more over the day: a row can only
    be dropped for repeating its predecessor, so the two passes compose.
    """
    order = np.argsort(cols["t"], kind="stable")
    order = order[np.argsort(cols["key"][order], kind="stable")]
    cols = {c: v[order] for c, v in cols.items()}
    if len(order) < 2:
        return cols
    same = np.ones(len(order), dtype=bool)
    same[0] = False
    grid = cols["t"] // (900 * 1_000_000_000)
    same[1:] &= grid[1:] == grid[:-1]
    for c in ("key", "bid", "ask", "bsz", "asz"):
        same[1:] &= cols[c][1:] == cols[c][:-1]
    return {c: v[~same] for c, v in cols.items()}


def _finish(parts: list[dict[str, np.ndarray]], nkeys: int, raw_rows: int, dedup: bool) -> Series:
    if not parts:
        empty = {c: np.empty(0, dtype=np.int64) for c in ("t", "key", "bid", "ask", "bsz", "asz")}
        z = np.zeros(nkeys, dtype=np.int64)
        return Series(empty, z, z.copy(), 0)
    cols = {c: np.concatenate([p[c] for p in parts]) for c in parts[0]}
    parts.clear()
    if dedup:
        cols = _dedup(cols)
    else:
        order = np.argsort(cols["t"], kind="stable")
        order = order[np.argsort(cols["key"][order], kind="stable")]
        cols = {c: v[order] for c, v in cols.items()}
    key = cols.pop("key")
    bounds = np.searchsorted(key, np.arange(nkeys + 1), side="left")
    return Series(cols, bounds[:-1], bounds[1:], raw_rows)


def kalshi_contract_start(t: np.ndarray, seq: np.ndarray, open_ns: int) -> int:
    """Index of the first row of the window's own Kalshi contract in one asset's rows.

    The Kalshi BBO stream is keyed by asset, not ticker. At each roll it keeps
    the old contract's terminal book (0 / 0.001) for 0.06-5 s past the open,
    then the new contract's quotes start, on a new subscription whose ``seq``
    restarts at 1 (2026-09-20: one restart per window, p50 2.6 s and p99 7.4 s
    after the open). A break is ``seq`` going down, or jumping by more than
    50,000 (a busy old contract's counter can be far past the new one's). The
    break nearest the open, within [open - 10 s, open + 60 s], is the switch;
    without one, the first row at or after the open is used.
    """
    lo = int(np.searchsorted(t, open_ns - 10 * 1_000_000_000))
    hi = int(np.searchsorted(t, open_ns + 60 * 1_000_000_000))
    at_open = int(np.searchsorted(t, open_ns))
    if hi - lo < 2:
        return at_open
    s = seq[lo:hi].astype(np.int64)
    d = np.diff(s)
    breaks = np.nonzero((d < 0) | (d > 50_000))[0] + 1 + lo
    if not len(breaks):
        return at_open
    return int(breaks[np.argmin(np.abs(t[breaks] - open_ns))])
