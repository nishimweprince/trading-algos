"""Content hash for a candle set.

Lives in ``harness/`` rather than ``cell_stats``, which is where it grew, for a
layering reason: ``api.py`` needs it on the request path -- every backtest report
carries ``candle_set_sha256`` -- and ``cell_stats`` imports the engine. Reaching
through an engine-aware analysis module to fingerprint a list of bars put a
production endpoint downstream of research code.

It is also on the determinism gate's hot path, so it is deliberately dull: sorted
keys, no whitespace, JSON mode so datetimes serialise the one documented way.
"""

from __future__ import annotations

import hashlib
import json

from ..models import Candle


def candle_sha256(candles: list[Candle]) -> str:
    payload = json.dumps(
        [candle.model_dump(mode="json") for candle in candles],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
