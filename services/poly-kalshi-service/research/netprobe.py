"""PLAN 5.1: round trip to each venue's API from this host, on kept-alive connections.

    python -m research.netprobe --minutes 60 [--every 1.0]

One HTTPS GET per venue per ``--every`` seconds on a single kept-alive
connection (TCP and TLS set up once), timed on the client: Polymarket's CLOB
``/time`` and Kalshi's ``/exchange/status``. Not ICMP: a ping would measure the
CDN's edge. Writes ``netprobe_<start>.json`` with p50/p99/max per venue. Run it on
each candidate host for 24 h; move only if p50 improves by > 2 ms or p99 by > 10 ms.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

TARGETS = {
    "polymarket_clob": "https://clob.polymarket.com/time",
    "kalshi": "https://api.elections.kalshi.com/trade-api/v2/exchange/status",
}


def _pct(values: list[float]) -> dict[str, float | int]:
    v = sorted(values)
    if not v:
        return {"n": 0}
    return {"n": len(v), "p50": round(v[len(v) // 2], 2), "p99": round(v[int(len(v) * 0.99)], 2),
            "max": round(v[-1], 2), "min": round(v[0], 2)}  # fmt: skip


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.netprobe")
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--every", type=float, default=1.0)
    parser.add_argument("--out", type=Path, default=Path("research/data/netprobe"))
    args = parser.parse_args()
    start = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rtt: dict[str, list[float]] = {k: [] for k in TARGETS}
    errors = dict.fromkeys(TARGETS, 0)
    clients = {k: httpx.Client(http2=False, timeout=5.0) for k in TARGETS}
    end = time.time() + args.minutes * 60
    try:
        while time.time() < end:
            for name, url in TARGETS.items():
                t0 = time.perf_counter()
                try:
                    clients[name].get(url).raise_for_status()
                    rtt[name].append((time.perf_counter() - t0) * 1000)
                except httpx.HTTPError:
                    errors[name] += 1
            time.sleep(args.every)
    finally:
        for c in clients.values():
            c.close()
    result = {"start": start, "host": socket.gethostname(), "minutes": args.minutes,
              "first_request_dropped": True,
              "rtt_ms": {k: _pct(v[1:]) for k, v in rtt.items()}, "errors": errors}  # fmt: skip
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"netprobe_{start}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    _main()
