"""Client for notification-service (``POST /notifications``).

The one hard rule, carried over from all the copies this replaces: **send never
raises.** A notification failure must not propagate into a trading path, so
every exception is logged and swallowed. That is deliberately the opposite of
the execution client's contract, where a silently dropped order submission is
the worst failure mode available.

``send`` returns a :class:`NotificationResult` rather than ``None``. That does
not weaken the never-raises rule -- it strengthens it, because a caller that
must record whether delivery happened previously had no way to find out. Callers
that ignore the return value are unaffected.

Two transports. :class:`Notifier` is async and reuses a shared
``httpx.AsyncClient``. :class:`SyncNotifier` is for callers with no event loop
at all -- lookup-trader runs its heartbeat monitor on a plain ``threading.Thread``
-- and builds a short-lived ``httpx.Client`` per call, which is what the code it
replaces did with ``urllib``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from ta_core import log_event

NOTIFICATIONS_PATH = "/notifications"

Status = Literal["sent", "remote_skipped", "failed", "disabled"]


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """What happened to one notification.

    ``remote_skipped`` is the service accepting the request and deciding not to
    deliver -- deduplication, a quiet window, a disabled channel. It is a
    success from the caller's point of view and is distinct from ``failed``,
    which means we do not know that anything arrived.
    """

    status: Status
    request_id: str | None = None
    error: str | None = None

    @property
    def delivered(self) -> bool:
        """True when the service took responsibility for the notification."""
        return self.status in {"sent", "remote_skipped"}


class SupportsNotification(Protocol):
    """The settings surface this client needs.

    A Protocol rather than a concrete class so a service can satisfy it with its
    own settings object -- which is how all the original copies were written,
    each against its own `Settings`.
    """

    notifications_enabled: bool
    notification_service_url: str
    notification_api_key: Any
    notification_timeout_seconds: float
    profile: str | None

    @property
    def notification_channels(self) -> frozenset[str]: ...


def _interpret(status_code: int, body: Any) -> NotificationResult:
    """Map one HTTP response onto a result. Shared by both transports."""
    if status_code == 201 and isinstance(body, dict) and body.get("requestId"):
        return NotificationResult("sent", str(body["requestId"]))
    if status_code == 200 and isinstance(body, dict) and body.get("status") == "skipped":
        request_id = body.get("requestId")
        return NotificationResult("remote_skipped", str(request_id) if request_id else None)
    if 200 <= status_code < 300:
        # Accepted, but not in a shape we recognise. Treat as sent rather than
        # failed: the service took it, and reporting failure here would make a
        # caller retry something that already went out.
        return NotificationResult("sent")
    return NotificationResult("failed", error=f"HTTP {status_code}")


class _NotifierBase:
    def __init__(self, settings: SupportsNotification, *, source: str) -> None:
        self._s = settings
        self._base_source = source
        self._url = f"{settings.notification_service_url.rstrip('/')}{NOTIFICATIONS_PATH}"

    @property
    def enabled(self) -> bool:
        return self._s.notifications_enabled and bool(self._s.notification_channels)

    @property
    def source(self) -> str:
        """Profile-suffixed so `ipda.forex` and `ipda.deriv` stay distinguishable."""
        if self._s.profile:
            return f"{self._base_source}.{self._s.profile}"
        return self._base_source

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._s.notification_api_key
        if api_key is not None:
            secret = api_key.get_secret_value()
            # Both headers: the service has accepted either across its history,
            # and sending both keeps older deployments working.
            headers["Authorization"] = f"Bearer {secret}"
            headers["X-API-Key"] = secret
        return headers

    def _payload(
        self, subject: str, lines: list[str], idempotency_key: str | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "message": "\n".join(lines),
            "contentType": "text",
            "channels": sorted(self._s.notification_channels),
            "source": self.source,
        }
        if idempotency_key is not None:
            # The service deduplicates on this, so a retry after an ambiguous
            # failure cannot produce a second alert.
            payload["idempotencyKey"] = idempotency_key
        return payload

    def _failed(self, subject: str, exc: Exception, context: dict[str, Any]) -> NotificationResult:
        # The exception *type* is logged, not its message. Taken from
        # lookup-trader's copy, which was written with the hazard in mind: an
        # exception raised while sending a request carrying an API key can
        # capture it in its message, and structured logs are shipped. The full
        # message is still on the returned result, where the caller can decide
        # what to do with it.
        log_event(
            "notification_failed",
            level=logging.WARNING,
            subject=subject,
            error=type(exc).__name__,
            **context,
        )
        return NotificationResult("failed", error=str(exc))


class Notifier(_NotifierBase):
    """Async transport, reusing a shared client."""

    def __init__(
        self,
        settings: SupportsNotification,
        client: httpx.AsyncClient,
        *,
        source: str,
    ) -> None:
        super().__init__(settings, source=source)
        self._client = client

    async def send(
        self,
        subject: str,
        lines: list[str],
        *,
        idempotency_key: str | None = None,
        **context: Any,
    ) -> NotificationResult:
        """Post one text notification. Never raises."""
        if not self.enabled:
            return NotificationResult("disabled")

        try:
            response = await self._client.post(
                self._url,
                json=self._payload(subject, lines, idempotency_key),
                headers=self._headers(),
                timeout=self._s.notification_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - notification must not affect trading
            return self._failed(subject, exc, context)

        return self._finish(subject, response, context)

    def _finish(
        self, subject: str, response: httpx.Response, context: dict[str, Any]
    ) -> NotificationResult:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is not a delivery failure
            body = None
        result = _interpret(response.status_code, body)
        if result.status == "failed":
            log_event(
                "notification_failed",
                level=logging.WARNING,
                subject=subject,
                error=result.error,
                **context,
            )
        return result


class SyncNotifier(_NotifierBase):
    """Blocking transport, for callers with no event loop.

    ``client`` is optional: without one a short-lived ``httpx.Client`` is built
    per call, which is what the ``urllib`` code this replaces did and what suits
    the callers this exists for -- threads and one-shot CLI scripts with no
    lifecycle to hang a shared client off. Pass one to reuse a connection, or to
    drive a ``MockTransport`` in tests.
    """

    def __init__(
        self,
        settings: SupportsNotification,
        client: httpx.Client | None = None,
        *,
        source: str,
    ) -> None:
        super().__init__(settings, source=source)
        self._client = client

    def send(
        self,
        subject: str,
        lines: list[str],
        *,
        idempotency_key: str | None = None,
        **context: Any,
    ) -> NotificationResult:
        """Post one text notification. Never raises."""
        if not self.enabled:
            return NotificationResult("disabled")

        try:
            if self._client is not None:
                response = self._client.post(
                    self._url,
                    json=self._payload(subject, lines, idempotency_key),
                    headers=self._headers(),
                    timeout=self._s.notification_timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._s.notification_timeout_seconds) as client:
                    response = client.post(
                        self._url,
                        json=self._payload(subject, lines, idempotency_key),
                        headers=self._headers(),
                    )
        except Exception as exc:  # noqa: BLE001 - notification must not affect trading
            return self._failed(subject, exc, context)

        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is not a delivery failure
            body = None
        result = _interpret(response.status_code, body)
        if result.status == "failed":
            log_event(
                "notification_failed",
                level=logging.WARNING,
                subject=subject,
                error=result.error,
                **context,
            )
        return result
