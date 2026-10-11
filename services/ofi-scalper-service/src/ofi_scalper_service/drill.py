"""``ofi-drill --profile dev disconnect|gap_handling``: run a drill on the live service.

Asks the running scalper (``POST /v1/drill/<name>``) to break its own feed:
``disconnect`` drops the public websocket mid-stream, ``gap_handling`` feeds
one book an out-of-sequence depth diff. The service checks that the pause goes
on, no entry is left resting, and the book resyncs; the result lands in
``<state_dir>/drills.json`` beside the kill-switch result, where ``ofi-status``
and ``ofi-export`` read it. Shadow only.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from .config import load_settings
from .runtime import DRILLS

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofi-drill", description=__doc__.split("\n")[0])
    parser.add_argument("--profile", default=None)
    parser.add_argument("name", choices=DRILLS)
    args = parser.parse_args(argv)
    settings = load_settings(args.profile)
    try:
        response = httpx.post(
            f"http://127.0.0.1:{settings.port}/v1/drill/{args.name}",
            headers={"X-API-Key": settings.api_key.get_secret_value()},
            timeout=180.0,
        )
    except httpx.HTTPError as exc:
        print(f"ofi-scalper-service unreachable on port {settings.port}: {exc}", file=sys.stderr)
        return 2
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    print(json.dumps(body, indent=2))
    if response.status_code != 200:
        return 2
    return 0 if body.get("result") == "passed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
