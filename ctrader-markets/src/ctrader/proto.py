"""The protobuf boundary: the only module that touches generated message code.

Everything else imports message classes and helpers from here, so swapping the
schema source stays a one-file change. See proto/README.md for why the schemas
are vendored and generated locally rather than taken from `ctrader-open-api`.
"""

from __future__ import annotations

import importlib

from google.protobuf.message import Message

from ctrader._generated.OpenApiCommonMessages_pb2 import (
    ProtoErrorRes,
    ProtoHeartbeatEvent,
    ProtoMessage,
)
from ctrader._generated.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAClientDisconnectEvent,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOARefreshTokenReq,
    ProtoOARefreshTokenRes,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASubscribeSpotsRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
    ProtoOAUnsubscribeSpotsReq,
)
from ctrader._generated.OpenApiModelMessages_pb2 import (
    ProtoOACtidTraderAccount,
    ProtoOALightSymbol,
    ProtoOASymbol,
    ProtoOATrendbar,
    ProtoOATrendbarPeriod,
)

_MODULES = (
    "OpenApiCommonMessages_pb2",
    "OpenApiMessages_pb2",
)


def _build_registry() -> dict[int, type[Message]]:
    """Map payloadType -> message class.

    Each request/response/event class carries its own payload type as the proto2
    default of its `payloadType` field, which is how the wire envelope is routed
    back to a concrete type. Model messages (ProtoOATrendbar, ProtoOALightSymbol,
    ProtoOACtidTraderAccount, ...) are nested values with no such field, and
    ProtoMessage itself declares the field as `required` with no default, so both
    are skipped rather than registered under 0.
    """
    registry: dict[int, type[Message]] = {}
    for module_name in _MODULES:
        module = importlib.import_module(f"._generated.{module_name}", __package__)
        for name in module.DESCRIPTOR.message_types_by_name:
            klass = getattr(module, name)
            if "payloadType" not in klass.DESCRIPTOR.fields_by_name:
                continue
            payload_type = int(klass().payloadType)
            if payload_type == 0:
                continue
            registry.setdefault(payload_type, klass)
    return registry


PAYLOAD_TYPE_TO_CLASS: dict[int, type[Message]] = _build_registry()


def payload_type_of(message: Message) -> int:
    """The wire payload type for an outbound message."""
    payload_type = int(getattr(message, "payloadType", 0))
    if payload_type == 0:
        raise ValueError(f"{type(message).__name__} carries no payloadType")
    return payload_type


def class_for_payload_type(payload_type: int) -> type[Message] | None:
    return PAYLOAD_TYPE_TO_CLASS.get(payload_type)


def parse_payload(payload_type: int, payload: bytes) -> Message | None:
    """Decode an envelope payload, or None when the payload type is unknown.

    Unknown types are returned as None rather than raised, so a schema addition
    on the broker side degrades to a logged-and-dropped frame instead of killing
    the reader loop.
    """
    klass = PAYLOAD_TYPE_TO_CLASS.get(payload_type)
    if klass is None:
        return None
    message = klass()
    message.ParseFromString(payload)
    return message


__all__ = [
    "PAYLOAD_TYPE_TO_CLASS",
    "ProtoErrorRes",
    "ProtoHeartbeatEvent",
    "ProtoMessage",
    "ProtoOAAccountAuthReq",
    "ProtoOAAccountAuthRes",
    "ProtoOAAccountsTokenInvalidatedEvent",
    "ProtoOAApplicationAuthReq",
    "ProtoOAApplicationAuthRes",
    "ProtoOAClientDisconnectEvent",
    "ProtoOACtidTraderAccount",
    "ProtoOAErrorRes",
    "ProtoOAGetAccountListByAccessTokenReq",
    "ProtoOAGetAccountListByAccessTokenRes",
    "ProtoOAGetTrendbarsReq",
    "ProtoOAGetTrendbarsRes",
    "ProtoOALightSymbol",
    "ProtoOARefreshTokenReq",
    "ProtoOARefreshTokenRes",
    "ProtoOASpotEvent",
    "ProtoOASubscribeSpotsReq",
    "ProtoOASubscribeSpotsRes",
    "ProtoOASymbol",
    "ProtoOASymbolByIdReq",
    "ProtoOASymbolByIdRes",
    "ProtoOASymbolsListReq",
    "ProtoOASymbolsListRes",
    "ProtoOATrendbar",
    "ProtoOATrendbarPeriod",
    "ProtoOAUnsubscribeSpotsReq",
    "class_for_payload_type",
    "parse_payload",
    "payload_type_of",
]
