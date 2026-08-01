"""Fire-once-then-lock de-duplication for mid-candle signals.

A signal may appear on any poll while a target-timeframe candle is still forming. We
emit the first one seen for a given bucket and then lock that bucket, so at most one
entry is produced per target candle regardless of intra-candle repainting.

The signal id is a deterministic UUIDv5 of (symbol, bucket start, direction), so a
transport retry replays idempotently against mt5-trader instead of creating a 409
idempotency conflict.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

# Stable namespace for this producer (random UUID4 frozen as a constant).
NAMESPACE = UUID("6f1d8f2e-3b4a-4c9d-9e2f-1a2b3c4d5e6f")


class SignalGate:
    def __init__(self) -> None:
        self._locked: set[tuple[str, datetime]] = set()

    def is_locked(self, symbol: str, bucket_start: datetime) -> bool:
        return (symbol, bucket_start) in self._locked

    def lock(self, symbol: str, bucket_start: datetime) -> None:
        self._locked.add((symbol, bucket_start))
        # Keep the set bounded: only the most recent buckets matter.
        if len(self._locked) > 512:
            for old in sorted(self._locked)[:-256]:
                self._locked.discard(old)


def signal_id_for(symbol: str, bucket_start: datetime, direction: str) -> UUID:
    key = f"lux-algo:{symbol}:{bucket_start.isoformat()}:{direction}"
    return uuid5(NAMESPACE, key)
