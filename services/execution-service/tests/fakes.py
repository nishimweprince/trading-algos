"""In-memory doubles for the cTrader connection.

No sockets and no event-loop I/O: the fake server feeds an asyncio.StreamReader
directly and records the exact bytes the client writes, so the framing layer is
asserted on the wire rather than through a mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from google.protobuf.message import Message

from execution_service.adapters.ctrader.framing import HEADER_BYTES, encode_frame
from execution_service.adapters.ctrader.proto import ProtoMessage, parse_payload, payload_type_of

Responder = Callable[[Message, str], Message | None]


class FakeStreamWriter:
    def __init__(self, server: FakeCTraderServer) -> None:
        self._server = server
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionResetError("writer closed")
        self.buffer.extend(data)
        self._server.on_client_bytes(bytes(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed


class SentFrame:
    __slots__ = ("client_msg_id", "message", "payload_type")

    def __init__(self, payload_type: int, message: Message | None, client_msg_id: str) -> None:
        self.payload_type = payload_type
        self.message = message
        self.client_msg_id = client_msg_id

    @property
    def name(self) -> str:
        return type(self.message).__name__ if self.message is not None else "unknown"

    def __repr__(self) -> str:
        return f"SentFrame({self.name}, client_msg_id={self.client_msg_id!r})"


class FakeCTraderServer:
    """A scriptable broker.

    responders maps a request message class name to a callable returning the
    response to feed back (or None to stay silent, for timeout tests).
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self.writer = FakeStreamWriter(self)
        self.responders: dict[str, Responder] = {}
        self.sent: list[SentFrame] = []
        self.connections = 0
        self._pending_bytes = bytearray()
        self._dropped = False

    @property
    def reader(self) -> asyncio.StreamReader:
        """Built on first use, never in __init__.

        asyncio.StreamReader() binds the running loop at construction; doing that
        outside one warns on 3.12 and raises on 3.14, which pyproject already
        allows (requires-python <3.15).
        """
        if self._reader is None:
            self._reader = asyncio.StreamReader()
        return self._reader

    @reader.setter
    def reader(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader

    def connector(self) -> Callable[[], object]:
        async def connect() -> tuple[asyncio.StreamReader, FakeStreamWriter]:
            # A fresh stream pair per connection. Reusing one reader would leave
            # it permanently at EOF after drop(), so no reconnect could succeed.
            self.connections += 1
            self.reader = asyncio.StreamReader()
            self.writer = FakeStreamWriter(self)
            self._pending_bytes = bytearray()
            self._dropped = False
            return self.reader, self.writer

        return connect

    # --- scripting -----------------------------------------------------------

    def respond(self, request_name: str, responder: Responder) -> None:
        self.responders[request_name] = responder

    def reply_with(self, request_name: str, response: Message) -> None:
        """Answer a request type with a fixed message, echoing the clientMsgId."""
        self.responders[request_name] = lambda _request, _mid: response

    def silence(self, request_name: str) -> None:
        self.responders[request_name] = lambda _request, _mid: None

    def push(self, message: Message, client_msg_id: str | None = None) -> None:
        """Inject an unsolicited event."""
        self.reader.feed_data(
            encode_frame(payload_type_of(message), message.SerializeToString(), client_msg_id)
        )

    def push_raw(self, data: bytes) -> None:
        self.reader.feed_data(data)

    def drop(self) -> None:
        """Simulate a broker-side disconnect."""
        if not self._dropped:
            self._dropped = True
            self.reader.feed_eof()

    # --- client -> server ----------------------------------------------------

    def on_client_bytes(self, data: bytes) -> None:
        self._pending_bytes.extend(data)
        while True:
            if len(self._pending_bytes) < HEADER_BYTES:
                return
            length = int.from_bytes(self._pending_bytes[:HEADER_BYTES], "big")
            if len(self._pending_bytes) < HEADER_BYTES + length:
                return
            body = bytes(self._pending_bytes[HEADER_BYTES : HEADER_BYTES + length])
            del self._pending_bytes[: HEADER_BYTES + length]
            self._handle(body)

    def _handle(self, body: bytes) -> None:
        envelope = ProtoMessage()
        envelope.ParseFromString(body)
        message = parse_payload(envelope.payloadType, envelope.payload)
        client_msg_id = envelope.clientMsgId if envelope.HasField("clientMsgId") else ""
        self.sent.append(SentFrame(envelope.payloadType, message, client_msg_id))

        if message is None:
            return
        responder = self.responders.get(type(message).__name__)
        if responder is None:
            return
        response = responder(message, client_msg_id)
        if response is None or self._dropped:
            return
        self.reader.feed_data(
            encode_frame(
                payload_type_of(response),
                response.SerializeToString(),
                client_msg_id or None,
            )
        )

    # --- assertions ----------------------------------------------------------

    def sent_names(self, *, exclude_heartbeats: bool = True) -> list[str]:
        frames = self.sent
        if exclude_heartbeats:
            frames = [f for f in frames if f.name != "ProtoHeartbeatEvent"]
        return [f.name for f in frames]

    def sent_of(self, request_name: str) -> list[SentFrame]:
        return [f for f in self.sent if f.name == request_name]

    def last_of(self, request_name: str) -> SentFrame:
        frames = self.sent_of(request_name)
        if not frames:
            raise AssertionError(f"{request_name} was never sent; saw {self.sent_names()}")
        return frames[-1]
