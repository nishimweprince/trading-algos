"""Fire-and-forget alerts through notification-service.

``ta_notify.Notifier.send`` never raises; this wrapper also never blocks the
caller (the send runs as a task) and throttles repeats of the same alert key so
a flapping stream cannot flood Telegram.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

__all__ = ["Alerts", "SupportsSend"]


class SupportsSend(Protocol):
    async def send(
        self, subject: str, lines: list[str], *, idempotency_key: str | None = None, **context: Any
    ) -> Any: ...


class Alerts:
    def __init__(
        self,
        notifier: SupportsSend | None,
        *,
        throttle_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifier = notifier
        self._throttle = throttle_seconds
        self._clock = clock
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.sent: list[tuple[str, str]] = []  # (key, subject), for status and tests
        self.suppressed = 0

    def send(self, key: str, subject: str, lines: list[str], *, always: bool = False) -> None:
        now = self._clock()
        last = self._last.get(key)
        if not always and last is not None and now - last < self._throttle:
            self.suppressed += 1
            return
        self._last[key] = now
        self.sent.append((key, subject))
        del self.sent[:-50]
        if self._notifier is None:
            return
        bucket = int(time.time() // max(self._throttle, 1))
        task = asyncio.create_task(
            self._notifier.send(subject, lines, idempotency_key=f"ofi:{key}:{bucket}")
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, seconds: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=seconds)
