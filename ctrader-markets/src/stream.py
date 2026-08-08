"""The SSE tick stream.

Uses sse-starlette's EventSourceResponse rather than a bare StreamingResponse.
A plain generator is only cancelled when it next tries to *yield*, so on a quiet
symbol a disconnected client's subscriber would stay registered indefinitely,
still being fed on every tick. EventSourceResponse races the generator against
the ASGI http.disconnect message, ships the keepalive ping, and cooperates with
uvicorn's graceful shutdown.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from hub import MarketDataHub, StreamEvent
from logging_config import log_event

# Nginx and friends buffer text/event-stream by default, which delays every
# event until the buffer fills. EventSourceResponse sets Cache-Control and
# Connection itself; this one it does not.
SSE_HEADERS = {"X-Accel-Buffering": "no"}


async def tick_stream(
    hub: MarketDataHub,
    symbols: frozenset[str] | None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE events for one subscriber until it disconnects."""
    subscriber = hub.register(symbols)
    try:
        yield _event(hub.status_event(subscriber))

        # Replay the last known quote per requested symbol. Without this, a
        # client joining mid-session waits an unbounded time for the first tick
        # on a quiet instrument.
        for symbol in sorted(symbols if symbols is not None else hub.known_symbols()):
            tick = hub.last_tick(symbol)
            if tick is not None:
                yield {"event": "tick", "data": tick.model_dump_json()}

        while True:
            event = await subscriber.queue.get()
            yield _event(event)
    finally:
        # The only cleanup path: covers normal completion, the CancelledError
        # raised on client disconnect, and shutdown.
        hub.unregister(subscriber)
        log_event(
            "stream_subscriber_closed",
            level=logging.INFO,
            console=False,
            dropped=subscriber.dropped,
            symbols=sorted(symbols) if symbols else None,
            remaining=hub.subscriber_count,
        )


def _event(event: StreamEvent) -> dict[str, Any]:
    return {"event": event.name, "data": json.dumps(event.payload, default=str)}
