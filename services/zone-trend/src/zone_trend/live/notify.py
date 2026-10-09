"""Alerts through the repo's notification service (``ta-notify``), routed by level.

``info`` goes to the configured ``NOTIFICATION_CHANNELS`` (Telegram by
default), ``warning`` to ``ZT_WARNING_CHANNELS``, ``critical`` to
``ZT_CRITICAL_CHANNELS``, each intersected with what the service allows.
Sending never raises (``ta-notify``'s contract); every alert is also logged
and written to the events table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from ta_notify import Notifier

from zone_trend.live.settings import LiveSettings
from zone_trend.persistence.repo import Repo

__all__ = ["AlertSink"]

log = logging.getLogger("zone_trend.alerts")
SOURCE = "zone-trend"


class AlertSink:
    def __init__(
        self,
        settings: LiveSettings,
        client: httpx.AsyncClient,
        repo: Repo | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.repo = repo
        self.clock = clock_ms
        self._by_level: dict[str, Notifier] = {}
        for level, csv in (
            ("info", settings.notification_channels_csv),
            ("warning", settings.zt_warning_channels_csv),
            ("critical", settings.zt_critical_channels_csv),
        ):
            copy = settings.model_copy(update={"notification_channels_csv": csv})
            self._by_level[level] = Notifier(copy, client, source=SOURCE)

    async def __call__(self, level: str, subject: str, lines: list[str]) -> None:
        log.log(
            {"info": logging.INFO, "warning": logging.WARNING}.get(level, logging.ERROR),
            "%s: %s | %s",
            level,
            subject,
            " / ".join(lines),
        )
        if self.repo is not None and self.clock is not None:
            self.repo.log_event(self.clock(), level, "alert", {"subject": subject, "lines": lines})
        notifier = self._by_level.get(level, self._by_level["critical"])
        tag = {"critical": "🚨 ", "warning": "⚠️ "}.get(level, "")
        await notifier.send(f"{tag}zone-trend: {subject}", lines)
