"""cTrader Open API wire framing: a 4-byte big-endian length prefix per message.

Pure functions plus one reader helper, so the framing layer is testable without
a socket. The reference client uses Twisted's Int32StringReceiver; the 2-byte
prefix shown in some of the HTML documentation examples is wrong.
"""

from __future__ import annotations

import asyncio
import struct

from ...errors import FrameError
from .proto import ProtoMessage

# Int32StringReceiver's MAX_LENGTH. A larger declared length means a desynced
# stream, so it is rejected before allocating a read of that size.
MAX_FRAME_BYTES = 15_000_000

_HEADER = struct.Struct(">I")
HEADER_BYTES = _HEADER.size


def encode_frame(payload_type: int, payload: bytes, client_msg_id: str | None = None) -> bytes:
    envelope = ProtoMessage(payloadType=payload_type, payload=payload)
    if client_msg_id is not None:
        envelope.clientMsgId = client_msg_id
    body = envelope.SerializeToString()
    return _HEADER.pack(len(body)) + body


def decode_frame(body: bytes) -> ProtoMessage:
    envelope = ProtoMessage()
    envelope.ParseFromString(body)
    return envelope


async def read_frame(reader: asyncio.StreamReader) -> ProtoMessage:
    """Read exactly one framed message.

    IncompleteReadError from a half-open socket propagates deliberately: it is
    the disconnect signal the supervisor reconnects on.
    """
    header = await reader.readexactly(HEADER_BYTES)
    (length,) = _HEADER.unpack(header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise FrameError(f"frame length {length} out of range")
    body = await reader.readexactly(length)
    return decode_frame(body)
