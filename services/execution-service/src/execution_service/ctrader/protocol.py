"""One cTrader TCP connection: framing loop, request correlation, heartbeat.

Knows nothing about authentication or market data. Owning exactly one
connection's lifetime is what lets session.py express the auth handshake as
straight-line awaits and treat reconnect as "throw this away and build another".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Awaitable, Callable
from uuid import uuid4

from google.protobuf.message import Message

from ..errors import CTraderError, CTraderTimeout
from ..logging_config import log_event
from .framing import encode_frame, read_frame
from .proto import (
    ProtoErrorRes,
    ProtoHeartbeatEvent,
    ProtoOAErrorRes,
    parse_payload,
    payload_type_of,
)

Connector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
EventHandler = Callable[[Message], None]
EnvelopeEventHandler = Callable[[Message, str | None], None]


def tls_connector(host: str, port: int) -> Connector:
    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(host, port, ssl=ssl.create_default_context())

    return connect


def _error_from(message: Message) -> CTraderError:
    return CTraderError(
        str(getattr(message, "errorCode", "UNKNOWN")),
        getattr(message, "description", None) or None,
    )


class CTraderProtocolClient:
    def __init__(
        self,
        connector: Connector,
        *,
        on_event: EventHandler,
        on_envelope_event: EnvelopeEventHandler | None = None,
        request_timeout: float = 10.0,
        heartbeat_interval: float = 5.0,
        connect_timeout: float = 15.0,
    ) -> None:
        self._connector = connector
        self._on_event = on_event
        self._on_envelope_event = on_envelope_event
        self._request_timeout = request_timeout
        self._heartbeat_interval = heartbeat_interval
        self._connect_timeout = connect_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[Message]] = {}
        self._send_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closed: asyncio.Future[Exception | None] | None = None
        self._closing = False

    async def connect(self) -> None:
        # asyncio.open_connection has no timeout of its own, and a peer that
        # completes the TCP handshake but stalls TLS leaves this awaiting
        # forever. That is worse than a failure: the supervisor never raises,
        # so it never backs off and never publishes an error, and /health/ready
        # reports "starting" with last_error null for the life of the process.
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._reader, self._writer = await self._connector()
        except TimeoutError as exc:
            raise CTraderTimeout(
                f"the broker connection was not established within {self._connect_timeout}s"
            ) from exc
        self._closing = False
        self._closed = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._read_loop(), name="ctrader-read")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ctrader-heartbeat")
        # A write failure kills only the heartbeat task, which would otherwise go
        # unnoticed until the broker drops us. Closing the writer instead unblocks
        # read_frame, so recovery runs through the single reconnect path.
        self._heartbeat_task.add_done_callback(self._on_heartbeat_done)

    async def request(
        self,
        message: Message,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - see docstring
        client_msg_id: str | None = None,
    ) -> Message:
        """Send a message and await the response carrying the same clientMsgId.

        The timeout is a parameter rather than the caller's `asyncio.timeout`
        because expiry has to unregister the pending entry and surface as
        CTraderTimeout; an external cancellation cannot do the first part.
        """
        client_msg_id = client_msg_id or uuid4().hex
        if client_msg_id in self._pending:
            raise RuntimeError(f"duplicate in-flight clientMsgId {client_msg_id}")
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[client_msg_id] = future
        deadline = timeout or self._request_timeout
        try:
            await self._write(message, client_msg_id)
            async with asyncio.timeout(deadline):
                return await future
        except TimeoutError as exc:
            raise CTraderTimeout(
                f"{type(message).__name__} was not answered within {deadline}s"
            ) from exc
        finally:
            # Unconditional, so a timeout or a cancellation cannot leak an entry.
            self._pending.pop(client_msg_id, None)

    async def send(self, message: Message) -> None:
        await self._write(message, None)

    async def wait_closed(self) -> Exception | None:
        """Resolve when the reader loop exits, with the error that ended it."""
        if self._closed is None:
            return None
        return await asyncio.shield(self._closed)

    @property
    def is_connected(self) -> bool:
        return self._closed is not None and not self._closed.done()

    async def close(self) -> None:
        """Idempotent: called from the supervisor's finally and from shutdown."""
        self._closing = True
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None:
                task.cancel()
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._heartbeat_task = None
        self._reader_task = None

        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            # A TLS transport can raise here when the peer already vanished;
            # that is not worth a traceback on every restart.
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None

        self._fail_pending(ConnectionResetError("client closed"))
        if self._closed is not None and not self._closed.done():
            self._closed.set_result(None)

    async def _write(self, message: Message, client_msg_id: str | None) -> None:
        if self._writer is None:
            raise ConnectionResetError("not connected")
        frame = encode_frame(payload_type_of(message), message.SerializeToString(), client_msg_id)
        # write() only buffers, so ordering is already safe; the lock keeps two
        # tasks from calling drain() concurrently.
        async with self._send_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def _read_loop(self) -> None:
        error: Exception | None = None
        try:
            while True:
                assert self._reader is not None
                envelope = await read_frame(self._reader)
                self._dispatch(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self._fail_pending(error or ConnectionResetError("connection closed"))
            if self._closed is not None and not self._closed.done():
                self._closed.set_result(error)

    def _dispatch(self, envelope: Message) -> None:
        message = parse_payload(envelope.payloadType, envelope.payload)
        if message is None:
            log_event(
                "unknown_payload_type",
                level=logging.DEBUG,
                console=False,
                payload_type=envelope.payloadType,
            )
            return

        client_msg_id = envelope.clientMsgId if envelope.HasField("clientMsgId") else ""
        future = self._pending.pop(client_msg_id, None) if client_msg_id else None
        if future is not None and not future.done():
            if isinstance(message, ProtoOAErrorRes | ProtoErrorRes):
                future.set_exception(_error_from(message))
            else:
                future.set_result(message)
            return

        if isinstance(message, ProtoHeartbeatEvent):
            return

        # Uncorrelated frames — spot events, and server-initiated errors such as
        # CH_ACCOUNT_NOT_AUTHORIZED or a maintenance window, which arrive with no
        # clientMsgId and are the session's cue to re-auth or back off.
        if self._on_envelope_event is not None:
            self._on_envelope_event(message, client_msg_id or None)
        else:
            self._on_event(message)

    async def _heartbeat_loop(self) -> None:
        """Send a heartbeat unconditionally on a fixed interval.

        The broker drops connections silent for 10s. Sending only when idle would
        mean sharing a last-write timestamp between this task and every request
        path for no benefit — a heartbeat is a few bytes against a 50 req/s budget.
        """
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await self._write(ProtoHeartbeatEvent(), None)

    def _on_heartbeat_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._closing:
            return
        exc = task.exception()
        if exc is None:
            return
        log_event(
            "heartbeat_failed",
            level=logging.WARNING,
            console=False,
            reason=type(exc).__name__,
        )
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()

    def _fail_pending(self, error: BaseException) -> None:
        """Resolve every in-flight request, or callers hang for a full timeout
        on each disconnect."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)
