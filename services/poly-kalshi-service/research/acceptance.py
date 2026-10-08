"""PLAN phase 2 acceptance: 24 h of the live service's own metrics, polled.

    python -m research.acceptance --hours 24 [--every 300] [--out research/data/acceptance]

Every ``--every`` seconds it reads ``/v1/status`` (``X-API-Key`` from the
profile's ``.env``) and keeps the rolling metrics. At the end it writes
``acceptance_<start>.json`` with the verdict per criterion:

- ``slow_consumer`` closes (all Polymarket sockets) <= 1 over the run;
- ``loop_lag_ms`` max < 250;
- ``age_ms_poly`` p99 < 150 (worst poll);
- ``engine_us`` p99 < 300 (worst poll, shadow variants on).

The metrics are rolling windows of the last 4,096 samples, so the worst of the
polls is the read. A failed poll (service down) is counted and fails the run.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

CRITERIA = {
    "loop_lag_ms": ("max", 250.0),
    "age_ms_poly": ("p99", 150.0),
    "engine_us": ("p99", 300.0),
}


def _api_key(env_file: Path) -> str:
    for line in env_file.read_text().splitlines():
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no API_KEY in {env_file}")


def verdict(polls: list[dict[str, Any]], failures: int) -> dict[str, Any]:
    worst: dict[str, float | None] = {}
    for name, (stat, _limit) in CRITERIA.items():
        values = [p["latency"][name][stat] for p in polls if name in p.get("latency", {})]
        worst[name] = max(values) if values else None
    slow = [p["slow_consumer"] for p in polls if p.get("slow_consumer") is not None]
    slow_delta = (slow[-1] - slow[0]) if len(slow) > 1 else None
    checks = {name: None if worst[name] is None else worst[name] < limit
              for name, (_s, limit) in CRITERIA.items()}  # fmt: skip
    checks["slow_consumer"] = None if slow_delta is None else slow_delta <= 1
    checks["no_failed_polls"] = failures == 0
    return {"worst": worst, "slow_consumer_closes": slow_delta, "failed_polls": failures,
            "polls": len(polls), "checks": checks,
            "pass": all(v is True for v in checks.values())}  # fmt: skip


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.acceptance")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--every", type=float, default=300.0)
    parser.add_argument("--url", default="http://127.0.0.1:8050")
    parser.add_argument("--env-file", type=Path, default=Path(".env.dev"))
    parser.add_argument("--out", type=Path, default=Path("research/data/acceptance"))
    args = parser.parse_args()
    key = _api_key(args.env_file)
    start = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    log = args.out / f"acceptance_{start}.jsonl"
    polls: list[dict[str, Any]] = []
    failures = 0
    end = time.time() + args.hours * 3600
    with httpx.Client(timeout=10.0, headers={"X-API-Key": key}) as http:
        while True:
            try:
                body = http.get(f"{args.url}/v1/status").json()
                poly = (body.get("feeds") or {}).get("poly") or {}
                poll = {"t": time.time(), "latency": body.get("latency") or {},
                        "slow_consumer": poly.get("slow_consumer"),
                        "loop": (body.get("host_load") or {}).get("loop")}  # fmt: skip
                polls.append(poll)
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                poll = {"t": time.time(), "error": f"{type(exc).__name__}: {exc}"[:200]}
            with log.open("a") as handle:
                handle.write(json.dumps(poll) + "\n")
            if time.time() >= end:
                break
            time.sleep(args.every)
    result = {"start": start, "hours": args.hours} | verdict(polls, failures)
    (args.out / f"acceptance_{start}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    _main()
