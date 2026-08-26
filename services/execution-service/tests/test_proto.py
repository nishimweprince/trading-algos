from __future__ import annotations

import pytest

from execution_service.adapters.ctrader import proto
from execution_service.adapters.ctrader.proto import (
    PAYLOAD_TYPE_TO_CLASS,
    ProtoHeartbeatEvent,
    ProtoMessage,
    ProtoOAApplicationAuthReq,
    ProtoOAErrorRes,
    ProtoOASpotEvent,
    ProtoOATrendbar,
    class_for_payload_type,
    parse_payload,
    payload_type_of,
)


def test_registry_populates() -> None:
    """Canary for protobuf runtime drift.

    If the generated modules stop importing or the payloadType defaults stop
    resolving, this fails in CI rather than at 3am on a reconnect.
    """
    assert len(PAYLOAD_TYPE_TO_CLASS) > 50


@pytest.mark.parametrize(
    "klass",
    [ProtoOASpotEvent, ProtoHeartbeatEvent, ProtoOAErrorRes, ProtoOAApplicationAuthReq],
)
def test_message_classes_are_registered_under_their_own_payload_type(klass: type) -> None:
    assert class_for_payload_type(payload_type_of(klass())) is klass


def test_no_twisted_import() -> None:
    """The whole reason the schemas are vendored. See proto/README.md."""
    import sys

    assert "twisted" not in sys.modules


def test_model_messages_without_payload_type_are_skipped_not_registered() -> None:
    assert "payloadType" not in ProtoOATrendbar.DESCRIPTOR.fields_by_name
    assert ProtoOATrendbar not in PAYLOAD_TYPE_TO_CLASS.values()


def test_envelope_itself_is_not_registered() -> None:
    """ProtoMessage declares payloadType as required with no default, so a naive
    registry would file it under 0 and shadow a real type."""
    assert 0 not in PAYLOAD_TYPE_TO_CLASS
    assert ProtoMessage not in PAYLOAD_TYPE_TO_CLASS.values()


def test_payload_type_of_rejects_a_message_without_one() -> None:
    with pytest.raises(ValueError, match="payloadType"):
        payload_type_of(ProtoOATrendbar())


def test_parse_payload_round_trips() -> None:
    event = ProtoOASpotEvent(ctidTraderAccountId=1, symbolId=42, bid=108532)

    parsed = parse_payload(payload_type_of(event), event.SerializeToString())

    assert isinstance(parsed, ProtoOASpotEvent)
    assert parsed.symbolId == 42
    assert parsed.bid == 108532


def test_parse_payload_returns_none_for_unknown_type() -> None:
    """A broker-side schema addition degrades to a dropped frame, not a crash."""
    assert parse_payload(999_999, b"") is None


def test_expected_message_classes_are_exported() -> None:
    for name in proto.__all__:
        assert hasattr(proto, name), name


def test_spot_event_bid_and_ask_are_optional() -> None:
    """The single most likely source of a silent bid=0.0 bug: cTrader only
    populates the side that changed, so decode must use HasField."""
    descriptor = ProtoOASpotEvent.DESCRIPTOR
    assert descriptor.fields_by_name["bid"].has_presence
    assert descriptor.fields_by_name["ask"].has_presence
    event = ProtoOASpotEvent(ctidTraderAccountId=1, symbolId=1, bid=100)
    assert event.HasField("bid")
    assert not event.HasField("ask")


def test_trendbar_uses_delta_encoding_from_low() -> None:
    bar = ProtoOATrendbar(volume=10, low=110000, deltaOpen=200, deltaHigh=500, deltaClose=100)
    assert bar.low + bar.deltaOpen == 110200
    assert bar.low + bar.deltaHigh == 110500
    assert bar.low + bar.deltaClose == 110100
