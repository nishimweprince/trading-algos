from __future__ import annotations

import asyncio
import struct

import pytest

from execution_service.adapters.ctrader.framing import (
    HEADER_BYTES,
    MAX_FRAME_BYTES,
    encode_frame,
    read_frame,
)
from execution_service.adapters.ctrader.proto import (
    ProtoHeartbeatEvent,
    ProtoMessage,
    payload_type_of,
)
from execution_service.errors import FrameError


def _reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def test_header_is_four_byte_big_endian_length() -> None:
    frame = encode_frame(51, b"", None)
    body = frame[HEADER_BYTES:]
    assert frame[:HEADER_BYTES] == struct.pack(">I", len(body))
    assert HEADER_BYTES == 4


async def test_round_trip_preserves_payload_type_and_client_msg_id() -> None:
    payload = ProtoHeartbeatEvent().SerializeToString()
    frame = encode_frame(51, payload, "abc123")

    envelope = await read_frame(_reader(frame))

    assert envelope.payloadType == 51
    assert envelope.clientMsgId == "abc123"
    assert envelope.payload == payload


async def test_client_msg_id_left_unset_when_none() -> None:
    """Not set at all, rather than set to the empty string.

    Dispatch keys on presence, so an empty-string id would collide with every
    other uncorrelated frame.
    """
    envelope = await read_frame(_reader(encode_frame(51, b"", None)))
    assert not envelope.HasField("clientMsgId")


async def test_frame_split_across_reads_is_reassembled() -> None:
    frame = encode_frame(payload_type_of(ProtoHeartbeatEvent()), b"\x08\x01", "split")
    reader = _reader(frame[:2], frame[2:6], frame[6:])

    envelope = await read_frame(reader)

    assert envelope.clientMsgId == "split"


async def test_two_frames_in_one_chunk_read_sequentially() -> None:
    reader = _reader(encode_frame(51, b"", "one") + encode_frame(51, b"", "two"))

    assert (await read_frame(reader)).clientMsgId == "one"
    assert (await read_frame(reader)).clientMsgId == "two"


async def test_oversized_length_rejected_before_reading_the_body() -> None:
    """Only the header is fed. If the guard were missing, readexactly would
    block on a body that is never coming."""
    reader = _reader(struct.pack(">I", MAX_FRAME_BYTES + 1), eof=False)

    with pytest.raises(FrameError, match="out of range"):
        await read_frame(reader)


async def test_zero_length_frame_rejected() -> None:
    with pytest.raises(FrameError, match="out of range"):
        await read_frame(_reader(struct.pack(">I", 0)))


async def test_truncated_body_raises_incomplete_read() -> None:
    frame = encode_frame(51, b"\x08\x01", "trunc")

    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(_reader(frame[:-1]))


async def test_truncated_header_raises_incomplete_read() -> None:
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(_reader(b"\x00\x00"))


def test_encode_accepts_a_full_envelope_round_trip() -> None:
    original = ProtoMessage(payloadType=2131, payload=b"\x10\x2a")
    frame = encode_frame(original.payloadType, original.payload, None)
    assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4
