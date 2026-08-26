from __future__ import annotations

import asyncio

import pytest
from google.protobuf.message import Message

from execution_service.ctrader.proto import (
    ProtoErrorRes,
    ProtoHeartbeatEvent,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOASpotEvent,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from execution_service.ctrader.protocol import CTraderProtocolClient
from execution_service.errors import CTraderError, CTraderTimeout
from tests.fakes import FakeCTraderServer


async def _client(
    server: FakeCTraderServer,
    events: list[Message] | None = None,
    **kwargs: float,
) -> CTraderProtocolClient:
    client = CTraderProtocolClient(
        server.connector(),  # type: ignore[arg-type]
        on_event=(events.append if events is not None else lambda _m: None),
        request_timeout=kwargs.get("request_timeout", 1.0),
        heartbeat_interval=kwargs.get("heartbeat_interval", 3600.0),
    )
    await client.connect()
    return client


async def test_request_resolves_on_the_matching_client_msg_id() -> None:
    server = FakeCTraderServer()
    server.reply_with("ProtoOAApplicationAuthReq", ProtoOAApplicationAuthRes())
    client = await _client(server)

    response = await client.request(ProtoOAApplicationAuthReq(clientId="a", clientSecret="b"))

    assert isinstance(response, ProtoOAApplicationAuthRes)
    assert server.last_of("ProtoOAApplicationAuthReq").client_msg_id
    await client.close()


async def test_request_can_use_a_deterministic_client_msg_id() -> None:
    server = FakeCTraderServer()
    server.reply_with("ProtoOAApplicationAuthReq", ProtoOAApplicationAuthRes())
    client = await _client(server)

    await client.request(
        ProtoOAApplicationAuthReq(clientId="a", clientSecret="b"),
        client_msg_id="operation-account-correlation",
    )

    assert (
        server.last_of("ProtoOAApplicationAuthReq").client_msg_id == "operation-account-correlation"
    )
    await client.close()


async def test_concurrent_requests_resolve_to_their_own_responses() -> None:
    """Replies are deliberately returned out of order."""
    server = FakeCTraderServer()
    held: list[tuple[Message, str]] = []
    both_seen = asyncio.Event()

    def capture(request: Message, client_msg_id: str) -> None:
        held.append((request, client_msg_id))
        if len(held) == 2:
            both_seen.set()
        return None

    server.respond("ProtoOASymbolsListReq", capture)
    client = await _client(server, request_timeout=5.0)

    first = asyncio.create_task(
        client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1, includeArchivedSymbols=False))
    )
    second = asyncio.create_task(
        client.request(ProtoOASymbolsListReq(ctidTraderAccountId=2, includeArchivedSymbols=True))
    )
    await asyncio.wait_for(both_seen.wait(), timeout=1.0)

    (_, first_id), (_, second_id) = held
    server.push(ProtoOASymbolsListRes(ctidTraderAccountId=2), second_id)
    server.push(ProtoOASymbolsListRes(ctidTraderAccountId=1), first_id)

    assert (await first).ctidTraderAccountId == 1
    assert (await second).ctidTraderAccountId == 2
    await client.close()


async def test_correlated_error_response_raises_with_the_error_code() -> None:
    server = FakeCTraderServer()
    server.reply_with(
        "ProtoOAApplicationAuthReq",
        ProtoOAErrorRes(errorCode="CH_CLIENT_AUTH_FAILURE", description="bad secret"),
    )
    client = await _client(server)

    with pytest.raises(CTraderError) as excinfo:
        await client.request(ProtoOAApplicationAuthReq(clientId="a", clientSecret="b"))

    assert excinfo.value.error_code == "CH_CLIENT_AUTH_FAILURE"
    assert excinfo.value.description == "bad secret"
    await client.close()


async def test_common_proto_error_res_also_raises() -> None:
    server = FakeCTraderServer()
    server.reply_with("ProtoOAApplicationAuthReq", ProtoErrorRes(errorCode="MAINTENANCE"))
    client = await _client(server)

    with pytest.raises(CTraderError, match="MAINTENANCE"):
        await client.request(ProtoOAApplicationAuthReq(clientId="a", clientSecret="b"))

    await client.close()


async def test_uncorrelated_error_goes_to_on_event_and_leaves_pending_alone() -> None:
    """Server-initiated errors carry no clientMsgId. They must not resolve an
    unrelated in-flight request."""
    server = FakeCTraderServer()
    server.silence("ProtoOASymbolsListReq")
    events: list[Message] = []
    client = await _client(server, events, request_timeout=5.0)

    pending = asyncio.create_task(client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1)))
    await asyncio.sleep(0)
    server.push(ProtoOAErrorRes(errorCode="CH_ACCOUNT_NOT_AUTHORIZED"))
    await asyncio.sleep(0.01)

    assert [type(e).__name__ for e in events] == ["ProtoOAErrorRes"]
    assert not pending.done()

    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await client.close()


async def test_spot_events_are_never_correlated() -> None:
    server = FakeCTraderServer()
    events: list[Message] = []
    client = await _client(server, events)

    server.push(ProtoOASpotEvent(ctidTraderAccountId=1, symbolId=7, bid=108532))
    await asyncio.sleep(0.01)

    assert len(events) == 1
    assert events[0].symbolId == 7
    await client.close()


async def test_inbound_heartbeat_is_swallowed() -> None:
    server = FakeCTraderServer()
    events: list[Message] = []
    client = await _client(server, events)

    server.push(ProtoHeartbeatEvent())
    await asyncio.sleep(0.01)

    assert events == []
    await client.close()


async def test_unknown_payload_type_is_dropped_not_fatal() -> None:
    server = FakeCTraderServer()
    events: list[Message] = []
    client = await _client(server, events)

    from execution_service.ctrader.framing import encode_frame

    server.push_raw(encode_frame(999_999, b"\x08\x01", None))
    await asyncio.sleep(0.01)
    server.push(ProtoOASpotEvent(ctidTraderAccountId=1, symbolId=3, bid=1))
    await asyncio.sleep(0.01)

    assert client.is_connected
    assert len(events) == 1
    await client.close()


async def test_timeout_raises_and_leaves_no_pending_entry() -> None:
    server = FakeCTraderServer()
    server.silence("ProtoOASymbolsListReq")
    client = await _client(server, request_timeout=0.05)

    with pytest.raises(CTraderTimeout, match="ProtoOASymbolsListReq"):
        await client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1))

    assert client._pending == {}
    await client.close()


async def test_cancelling_a_request_leaves_no_pending_entry() -> None:
    server = FakeCTraderServer()
    server.silence("ProtoOASymbolsListReq")
    client = await _client(server, request_timeout=5.0)

    task = asyncio.create_task(client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1)))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert client._pending == {}
    await client.close()


async def test_disconnect_fails_every_pending_request() -> None:
    server = FakeCTraderServer()
    server.silence("ProtoOASymbolsListReq")
    client = await _client(server, request_timeout=5.0)

    first = asyncio.create_task(client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1)))
    second = asyncio.create_task(client.request(ProtoOASymbolsListReq(ctidTraderAccountId=2)))
    await asyncio.sleep(0.01)
    server.drop()

    for task in (first, second):
        with pytest.raises(Exception):  # noqa: B017 - any failure beats hanging
            await task
    await client.close()


async def test_wait_closed_resolves_with_the_reader_error() -> None:
    server = FakeCTraderServer()
    client = await _client(server)

    server.drop()
    error = await asyncio.wait_for(client.wait_closed(), timeout=1.0)

    assert isinstance(error, asyncio.IncompleteReadError)
    assert not client.is_connected
    await client.close()


async def test_heartbeat_is_written_on_the_interval() -> None:
    server = FakeCTraderServer()
    client = await _client(server, heartbeat_interval=0.01)

    await asyncio.sleep(0.05)

    assert len(server.sent_of("ProtoHeartbeatEvent")) >= 2
    await client.close()


async def test_close_is_idempotent_and_leaves_no_tasks() -> None:
    server = FakeCTraderServer()
    client = await _client(server, heartbeat_interval=0.01)
    before = {t.get_name() for t in asyncio.all_tasks()}

    await client.close()
    await client.close()

    remaining = {t.get_name() for t in asyncio.all_tasks()} - before
    assert not {name for name in remaining if name.startswith("ctrader-")}
    assert client._pending == {}


async def test_request_after_close_fails_fast() -> None:
    server = FakeCTraderServer()
    client = await _client(server)
    await client.close()

    with pytest.raises(ConnectionResetError):
        await client.request(ProtoOASymbolsListReq(ctidTraderAccountId=1))


async def test_send_is_fire_and_forget_without_a_client_msg_id() -> None:
    server = FakeCTraderServer()
    client = await _client(server)

    await client.send(ProtoHeartbeatEvent())

    assert server.last_of("ProtoHeartbeatEvent").client_msg_id == ""
    await client.close()


# --- connect timeout ---------------------------------------------------------


async def test_a_stalled_connector_times_out_rather_than_hanging_forever() -> None:
    """asyncio.open_connection has no timeout of its own.

    Found by running the service against the real broker endpoint: TCP connected
    in ~1s and the TLS handshake then stalled, so the supervisor sat inside
    connect() indefinitely. It never raised, so it never backed off and never
    published an error — /health/ready reported "starting" with last_error null
    for as long as the process lived, which is the one failure mode the design
    is explicitly trying to avoid.
    """
    started = asyncio.Event()

    async def never_connects() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    client = CTraderProtocolClient(
        never_connects,
        on_event=lambda _message: None,
        connect_timeout=0.05,
    )

    with pytest.raises(CTraderTimeout, match="not established within"):
        await client.connect()

    assert started.is_set()
    assert not client.is_connected


async def test_a_connect_timeout_is_retryable() -> None:
    """The supervisor treats it as an ordinary failure and tries again, so a
    timed-out attempt must not leave the client wedged."""
    attempts = 0
    server = FakeCTraderServer()

    async def slow_then_fine() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(3600)
        return await server.connector()()  # type: ignore[no-any-return]

    client = CTraderProtocolClient(
        slow_then_fine,
        on_event=lambda _message: None,
        connect_timeout=0.05,
    )
    with pytest.raises(CTraderTimeout):
        await client.connect()

    await client.connect()

    assert client.is_connected
    await client.close()
