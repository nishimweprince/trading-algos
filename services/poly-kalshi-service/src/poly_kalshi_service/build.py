"""Which code and which settings produced a record: stamped on every journal line.

``BUILD`` is the git commit of the checkout (``PK_BUILD`` overrides it, e.g. in a
packaged deploy). ``config_hash`` fingerprints the strategy parameters.
Together with the results epoch (``PK_RESULTS_EPOCH``) they let results be
read only for the code and settings they came from: the ghost-level bug left
fifteen entries in a journal that otherwise looked like any other.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from functools import cache
from pathlib import Path

from .decision import StrategyParams

__all__ = ["build_id", "config_hash"]


@cache
def build_id() -> str:
    if os.environ.get("PK_BUILD"):
        return os.environ["PK_BUILD"]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def config_hash(
    params: StrategyParams,
    latency_ms: int,
    limit_offset: float = 0.0,
    size_to_depth: bool = False,
) -> str:
    # Optional parameters that are unset are left out, so adding one does not
    # change the hash of every configuration that does not use it.
    fields = {k: v for k, v in asdict(params).items() if v is not None}
    fields["paper_latency_ms"] = latency_ms
    if limit_offset:
        fields["limit_offset"] = limit_offset
    if size_to_depth:
        fields["size_to_depth"] = True
    body = json.dumps(fields, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:10]
