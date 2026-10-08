"""PLAN 0.1b: one row per Helix stream per day, and each day usable or excluded.

    python -m research.helix.inventory [--checksums kpc_tier2.sha256.log]

Writes ``research/data/helix_inventory.json``:

- ``streams``: per stream per day, from the block headers alone (no
  decompression): records, blocks, sub-files (recorder restarts), the time span
  and the **largest internal gap** (between consecutive blocks' time ranges, in
  the block clock), the file size, and the checksum result where the manifest
  covers the file (``kpc_tier2.sha256`` covers trades and windows, not BBO).
- ``assets``: per day, the windows and BBO rows per asset, from the replay's
  ``windows.parquet`` (the BBO streams are only ever read through the replay).
- ``days``: per day, the settlement regime, the role in research (``discovery``,
  ``holdout``, ``earlier_regime``) and ``usable`` with the reason when not.

Discovery and holdout (PLAN 3.3): the TWAP-regime days except the most recent
14 are discovery; the most recent 14 are the holdout. The earlier regime's days
are reported separately and never set a parameter.
"""

from __future__ import annotations

import argparse
import json
import mmap
import struct
from pathlib import Path
from typing import Any

from . import reader, recv3

HOLDOUT_DAYS = 14
MIN_KALSHI_HOURS = 20.0


def _footer_blocks(p: Path) -> list[tuple[int, int, int, int, int, int]] | None:
    """``(offset, comp, count, tag, first, last)`` per block from the footer index, if the
    file is one sub-file: the indexed blocks must tile it from byte 32 to the index.
    Two small reads instead of a walk over every block header."""
    size = p.stat().st_size
    with p.open("rb") as fh:
        head = fh.read(32)
        if len(head) < 32 or size < 32 + 24:
            return None
        flags = recv3._FILE_HDR.unpack_from(head, 0)[6]
        if not flags & recv3._FOOTER_PRESENT:
            return None
        fh.seek(size - 24)
        fmag, n, ilen, _total = recv3._TRAILER.unpack(fh.read(24))
        if fmag != recv3._FOOT_MAGIC or ilen != n * 40:
            return None
        idx_off = size - 24 - ilen
        fh.seek(idx_off)
        raw = fh.read(ilen)
    blocks = []
    expect = 32
    for k in range(n):
        off, comp, count, tag, _p, first, last = recv3._IDX_ENT.unpack_from(raw, k * 40)
        if off != expect:
            return None  # a gap: another sub-file or footer sits in between
        blocks.append((off, comp, count, tag, first, last))
        expect = off + 40 + comp
    return blocks if expect == idx_off else None


WALK_MAX_BYTES = 300_000_000


def scan_headers(p: Path, walk_max: int | None = None) -> dict[str, Any]:
    """Block counts, span and largest gap: from the footer index when it covers the
    whole file, else by walking the block headers (readahead off)."""
    indexed = _footer_blocks(p)
    if indexed is None:
        size = p.stat().st_size
        if walk_max is not None and size > walk_max:
            # A restart day without a whole-file index: walking ~50k block headers is
            # IOPS-bound (minutes per file). Its decoded rows are in the replay summary.
            return {"bytes": size, "indexed": False, "note": "not indexed (recorder restarts)"}
        return _walk(p)
    tags: dict[int, int] = {}
    for _o, _c, count, tag, _f, _l in indexed:
        tags[tag] = tags.get(tag, 0) + count
    spans = [(first, last) for *_x, first, last in indexed]
    records = sum(b[2] for b in indexed)
    return _summary(p.stat().st_size, records, len(indexed), 1, tags, spans)


def _summary(
    size: int, records: int, blocks: int, subs: int, tags: dict[int, int], spans: list
) -> dict[str, Any]:
    gap = 0
    if spans:
        spans = sorted(spans)
        reach = spans[0][1]
        for first, last in spans[1:]:
            gap = max(gap, first - reach)
            reach = max(reach, last)
    return {
        "bytes": size,
        "records": records,
        "blocks": blocks,
        "subfiles": subs,
        "tags": tags,
        "span_s": None if not spans else round((max(s[1] for s in spans) - spans[0][0]) / 1e9, 1),
        "largest_gap_s": round(gap / 1e9, 1),
    }


def _walk(p: Path) -> dict[str, Any]:
    with p.open("rb") as fh:
        data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    # Only the 40 B headers are read: without this, readahead pulls in the whole file.
    data.madvise(mmap.MADV_RANDOM)
    magic = struct.pack("<I", recv3._FILE_MAGIC)
    off, n = 32, len(data)
    records = blocks = 0
    subs = 1
    spans: list[tuple[int, int]] = []
    tags: dict[int, int] = {}
    while off + 40 <= n:
        if struct.unpack_from("<I", data, off)[0] == recv3._FILE_MAGIC:
            off += 32
            subs += 1
            continue
        bm, tag, _p, _rsz, count, comp, _u, first, last = recv3._BLK_HDR.unpack_from(data, off)
        if bm != recv3._BLK_MAGIC:
            nxt = data.find(magic, off + 4)
            if nxt < 0:
                break
            off = nxt
            continue
        if off + 40 + comp > n:
            break
        blocks += 1
        records += count
        tags[tag] = tags.get(tag, 0) + count
        spans.append((first, last))
        off += 40 + comp
    return _summary(n, records, blocks, subs, tags, spans)


def _failures(log: Path | None) -> set[str] | None:
    """Files ``sha256sum --quiet -c`` reported (it prints failures only); None if not run."""
    if log is None or not log.is_file():
        return None
    out = set()
    for line in log.read_text().splitlines():
        name, _, verdict = line.rpartition(": ")
        if name and verdict.strip() != "OK":
            out.add(name.strip())
    return out


def _checksum(key: str, manifest: set[str], failures: set[str] | None) -> str:
    if key not in manifest:
        return "not in manifest"
    if failures is None:
        return "unchecked"
    return "FAILED" if key in failures else "OK"


def build(
    root: Path,
    replay_dir: Path,
    resolutions: Path,
    failures: set[str] | None,
    manifest: set[str],
) -> dict[str, Any]:
    import pandas as pd

    streams: dict[str, dict[str, Any]] = {}
    for stream in reader.STREAMS:
        for day in reader.days(stream, root):
            row = scan_headers(reader.path(stream, day, root), WALK_MAX_BYTES)
            row["checksum"] = _checksum(f"{stream}/{day}.hlxz", manifest, failures)
            streams.setdefault(stream, {})[day] = row

    regimes: dict[str, str] = {}
    if resolutions.is_file():
        res = pd.read_parquet(resolutions)
        for day, g in res.groupby("day"):
            counts = g["regime"].value_counts()
            regimes[str(day)] = str(counts.idxmax()) if len(counts) else "unknown"

    assets: dict[str, Any] = {}
    kalshi_hours: dict[str, float] = {}
    for day_dir in sorted(replay_dir.glob("2*")):
        wpath = day_dir / "windows.parquet"
        if not wpath.is_file():
            continue
        w = pd.read_parquet(wpath)
        summary = json.loads((day_dir / "summary.json").read_text())
        for stream, key in (("Polymarket_CLOB_Yes_BBO", "poly_yes_raw"),
                            ("Kalshi_Yes_BBO", "kalshi_raw")):  # fmt: skip
            row = streams.get(stream, {}).get(day_dir.name)
            if row is not None:
                row["decoded_rows_15m"] = summary["rows"].get(key)
        assets[day_dir.name] = {
            a: {
                "windows": int(len(g)),
                "with_kalshi": int((g["k_rows"] > 0).sum()),
                "with_poly": int(((g["y_rows"] > 0) & (g["n_rows"] > 0)).sum()),
                "kalshi_rows": int(g["k_rows"].sum()),
                "poly_yes_rows": int(g["y_rows"].sum()),
            }
            for a, g in w.groupby("asset")
        }
        kalshi_hours[day_dir.name] = float((w.groupby("open_unix")["k_rows"].max() > 0).sum()) / 4

    poly_days = reader.days("Polymarket_Window", root)
    twap = [d for d in poly_days if regimes.get(d) == "twap"]
    holdout = set(twap[-HOLDOUT_DAYS:])
    days: dict[str, Any] = {}
    for day in poly_days:
        reasons = []
        for need in ("Polymarket_CLOB_Yes_BBO", "Polymarket_CLOB_No_BBO", "Kalshi_Yes_BBO"):
            if day not in streams.get(need, {}):
                reasons.append(f"no {need}")
        if day in kalshi_hours and kalshi_hours[day] < MIN_KALSHI_HOURS:
            reasons.append(f"Kalshi covers {kalshi_hours[day]:.1f} h of windows")
        bad = [s for s, d in streams.items() if d.get(day, {}).get("checksum") == "FAILED"]
        if bad:
            reasons.append(f"checksum failed: {', '.join(bad)}")
        regime = regimes.get(day, "unknown")
        role = (
            "holdout" if day in holdout else "discovery" if regime == "twap" else "earlier_regime"
        )
        days[day] = {
            "regime": regime,
            "role": role,
            "kalshi_window_hours": kalshi_hours.get(day),
            "usable": not reasons,
            "reasons": reasons or ["usable"],
            "chainlink": day in streams.get("PolymarketChainlink_BBO", {}),
        }
    return {"streams": streams, "assets": assets, "days": days}


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix.inventory")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--replay", type=Path, default=Path("research/data/helix"))
    parser.add_argument(
        "--resolutions", type=Path, default=Path("research/data/resolutions.parquet")
    )
    parser.add_argument("--manifest", type=Path, default=Path.home() / "kpc" / "kpc_tier2.sha256")
    parser.add_argument("--checksums", type=Path, default=None, help="sha256sum -c output")
    parser.add_argument("--out", type=Path, default=Path("research/data/helix_inventory.json"))
    args = parser.parse_args()
    root = reader.tape_dir(args.root)
    manifest = (
        {line.split(None, 1)[1].strip() for line in args.manifest.read_text().splitlines() if line}
        if args.manifest.is_file()
        else set()
    )
    body = build(root, args.replay, args.resolutions, _failures(args.checksums), manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(body, indent=1))
    for day, d in body["days"].items():
        print(day, d["regime"], d["role"], "usable" if d["usable"] else "; ".join(d["reasons"]))


if __name__ == "__main__":
    _main()
