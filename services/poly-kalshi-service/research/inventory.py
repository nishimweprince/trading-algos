"""PLAN 0.1c: our tape, one row per recorded hour file, usable or excluded and why.

    python -m research.inventory --record-dir data/raw/dev [--journal data/state/dev/journal]

Writes ``research/data/inventory.json``. Per file: host (from the manifest),
lines per venue, sessions, ``out_of_order`` and ``truncated`` (read through
``recordings.read_lines``, so the counts are the replay's), the session start,
and two flags:

- ``ghost_levels``: the session started before ``3bfa696`` (Kalshi's cancelled
  levels left ~1e-14 ghosts that crossed the book). Excluded from all research.
- ``pre_seq_fix``: the session started before ``1721a36`` (Kalshi seq counted
  only orderbook frames, forcing two reconnects a window). Excluded from fill
  research; fine for signal statistics.

``feed_ms_k`` / ``feed_ms_p`` (p10/p50 of receive minus venue time, 1 line in
``SAMPLE``) are the file's measured geometry. They, not the host tag, say where it
was recorded: ``host_by_geometry`` is ``london`` for Polymarket p50 under 4 ms,
``dublin`` for 6 to 20 ms. The tag was ``london`` on the Dublin host for its first
sessions, which the geometry shows.

A session's code is inferred from its start time against the commit time, and
confirmed from the ``build`` stamped on the journal records written during the
file's own time range (a deploy can lag its commit). Re-timing: London hours
stand for Dublin only after ``research.replay run --shift-ms`` (PLAN 3.1).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.trades import read_journal

NS = 1_000_000_000
SAMPLE = 20
# Commit times (UTC) of the fixes that split the tape.
GHOST_FIX = ("3bfa696", datetime(2026, 10, 6, 17, 31, 51, tzinfo=UTC))
SEQ_FIX = ("1721a36", datetime(2026, 10, 6, 18, 33, 3, tzinfo=UTC))


def _stamps(journal: Path | None) -> list[tuple[int, str]]:
    """``(t_ns, build)`` of every journal record that has both, in time order."""
    if journal is None or not journal.is_dir():
        return []
    out = [(r["t_ns"], r["build"][:7]) for r in read_journal(journal)
           if r.get("t_ns") and r.get("build")]  # fmt: skip
    return sorted(out)


def _venue_ms(venue: str, text: str) -> float | None:
    """The venue's own timestamp (ms) on a Kalshi orderbook or Polymarket market frame."""
    try:
        body = orjson.loads(text)
    except orjson.JSONDecodeError:
        return None
    if venue == "k":
        if not isinstance(body, dict):
            return None
        value = (body.get("msg") or {}).get("ts_ms") or body.get("sending_ts_ms")
    else:
        msg = body[0] if isinstance(body, list) and body else body
        value = msg.get("timestamp") if isinstance(msg, dict) else None
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _pcts(values: list[float]) -> dict[str, float] | None:
    if len(values) < 20:
        return None
    values = sorted(values)
    return {"n": len(values), "p10": round(values[len(values) // 10], 2),
            "p50": round(values[len(values) // 2], 2)}  # fmt: skip


def _iso(t_ns: int | None) -> str | None:
    if t_ns is None:
        return None
    return datetime.fromtimestamp(t_ns / NS, UTC).isoformat(timespec="seconds")


def inventory(record_dir: Path, journal: Path | None) -> list[dict[str, Any]]:
    stamps = _stamps(journal)
    # A file still being written has no manifest yet: its session's other files name the host.
    session_host: dict[str, str] = {}
    for m in record_dir.rglob("pk_*.gz.json"):
        host = json.loads(m.read_text()).get("host")
        if host:
            session_host[m.name.split("_")[-1].split(".")[0]] = host
    rows = []
    for path in iter_files(record_dir):
        manifest_path = path.with_name(path.name + ".json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        stats: dict[str, int] = {}
        per_venue: dict[str, int] = {}
        lat: dict[str, list[float]] = {"k": [], "p": []}
        first = last = None
        try:
            for recv_ns, venue, text in read_lines([path], stats):
                n = per_venue[venue] = per_venue.get(venue, 0) + 1
                first = recv_ns if first is None else first
                last = recv_ns
                if venue in lat and n % SAMPLE == 0:
                    venue_ms = _venue_ms(venue, text)
                    if venue_ms:
                        lat[venue].append(recv_ns / 1e6 - venue_ms)
        except ValueError:
            pass  # no readable line: reported through the counts below
        session_ns = int(path.stem.split("_")[-1].split(".")[0])
        session_start = datetime.fromtimestamp(session_ns / NS, UTC)
        hour = path.name[3:14]
        # Builds of the records written while this file was: a deploy can lag its commit.
        seen = sorted({b for t, b in stamps if first is not None and first <= t <= (last or 0)})
        ghost = session_start < GHOST_FIX[1] and GHOST_FIX[0] not in seen
        pre_seq = session_start < SEQ_FIX[1] and SEQ_FIX[0] not in seen
        reasons = []
        if ghost:
            reasons.append("ghost Kalshi levels (before 3bfa696): excluded")
        elif pre_seq:
            reasons.append("before the Kalshi seq fix (1721a36): signal only, no fill research")
        if stats.get("truncated"):
            reasons.append("truncated (crash or still being written): read to the break")
        if stats.get("out_of_order"):
            reasons.append(f"{stats['out_of_order']} lines out of order")
        host = manifest.get("host") or session_host.get(path.stem.split("_")[-1], "unknown")
        geo = {v: _pcts(x) for v, x in lat.items()}
        p50 = (geo["p"] or {}).get("p50")
        by_geometry = (
            None if p50 is None else "london" if p50 < 4 else "dublin" if p50 < 20 else "?"
        )
        if by_geometry == "london":
            reasons.append("London geometry: re-time to Dublin before any fill number (PLAN 3.1)")
        if by_geometry and by_geometry != host:
            reasons.append(f"tagged {host}, but its latencies are {by_geometry}'s")
        rows.append(
            {
                "file": str(path.relative_to(record_dir)),
                "hour": hour,
                "host": host,
                "session_start": session_start.isoformat(timespec="seconds"),
                "manifest": bool(manifest),
                "sessions": manifest.get("sessions"),
                "lines": stats.get("lines", 0),
                "lines_per_venue": per_venue,
                "first_recv": _iso(first),
                "last_recv": _iso(last),
                "out_of_order": stats.get("out_of_order", 0),
                "truncated": stats.get("truncated", 0),
                "bad_lines": stats.get("bad", 0),
                "journal_builds": seen,
                "feed_ms_k": geo["k"],
                "feed_ms_p": geo["p"],
                "host_by_geometry": by_geometry,
                "ghost_levels": ghost,
                "pre_seq_fix": pre_seq,
                "usable_for_signal": not ghost,
                "usable_for_fills": not ghost and not pre_seq and by_geometry == "dublin",
                "reasons": reasons or ["usable"],
            }
        )  # fmt: skip
    return rows


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.inventory")
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("research/data/inventory.json"))
    args = parser.parse_args()
    rows = inventory(args.record_dir, args.journal)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=1))
    for r in rows:
        geo = f"k={(r['feed_ms_k'] or {}).get('p50')} p={(r['feed_ms_p'] or {}).get('p50')}"
        print(f"{r['hour']} {r['host']:7} {geo:22} {r['lines']:>9} ooo={r['out_of_order']} "
              f"trunc={r['truncated']} fills={'Y' if r['usable_for_fills'] else 'n'} "
              f"{'; '.join(r['reasons'])}")  # fmt: skip


if __name__ == "__main__":
    _main()
