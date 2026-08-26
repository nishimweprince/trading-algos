from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution_service.config import Settings
from execution_service.ctrader.proto import (
    ProtoOAAccountAuthRes,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAApplicationAuthRes,
    ProtoOAClientDisconnectEvent,
    ProtoOAErrorRes,
    ProtoOAGetTrendbarsRes,
    ProtoOALightSymbol,
    ProtoOARefreshTokenRes,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsRes,
    ProtoOASymbol,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListRes,
    ProtoOATrendbar,
)
from execution_service.ctrader.session import CTraderSession
from execution_service.hub import MarketDataHub
from execution_service.models import Timeframe
from tests.conftest import build_settings
from tests.fakes import FakeCTraderServer

ACCOUNT_ID = 12345678


def happy_server(*, symbols: tuple[str, ...] = ("EURUSD", "XAUUSD")) -> FakeCTraderServer:
    server = FakeCTraderServer()
    server.reply_with("ProtoOAApplicationAuthReq", ProtoOAApplicationAuthRes())
    server.reply_with(
        "ProtoOAAccountAuthReq", ProtoOAAccountAuthRes(ctidTraderAccountId=ACCOUNT_ID)
    )
    server.reply_with(
        "ProtoOASymbolsListReq",
        ProtoOASymbolsListRes(
            ctidTraderAccountId=ACCOUNT_ID,
            symbol=[
                ProtoOALightSymbol(symbolId=i + 1, symbolName=name, enabled=True)
                for i, name in enumerate(symbols)
            ],
        ),
    )
    server.reply_with(
        "ProtoOASymbolByIdReq",
        ProtoOASymbolByIdRes(
            ctidTraderAccountId=ACCOUNT_ID,
            symbol=[
                # pipPosition is a required field on ProtoOASymbol even though
                # only digits is read here.
                ProtoOASymbol(
                    symbolId=i + 1,
                    digits=5 if name != "XAUUSD" else 2,
                    pipPosition=4 if name != "XAUUSD" else 1,
                )
                for i, name in enumerate(symbols)
            ],
        ),
    )
    server.reply_with(
        "ProtoOASubscribeSpotsReq", ProtoOASubscribeSpotsRes(ctidTraderAccountId=ACCOUNT_ID)
    )
    return server


async def start_session(
    server: FakeCTraderServer, settings: Settings, hub: MarketDataHub | None = None
) -> tuple[CTraderSession, MarketDataHub]:
    hub = hub or MarketDataHub(queue_size=32)
    session = CTraderSession(settings, hub, connector=server.connector())  # type: ignore[arg-type]
    await session.start()
    await session.wait_ready(timeout_seconds=2.0)
    return session, hub


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        RECONNECT_MAX_BACKOFF_SECONDS=0.02,
        REQUEST_TIMEOUT_SECONDS=1,
    )


# --- handshake ---------------------------------------------------------------


async def test_handshake_sends_the_requests_in_order(settings: Settings) -> None:
    server = happy_server()

    session, _ = await start_session(server, settings)

    assert server.sent_names() == [
        "ProtoOAApplicationAuthReq",
        "ProtoOAAccountAuthReq",
        "ProtoOASymbolsListReq",
        "ProtoOASymbolByIdReq",
        "ProtoOASubscribeSpotsReq",
    ]
    assert session.is_ready
    await session.close()


async def test_subscribes_to_every_configured_symbol(settings: Settings) -> None:
    server = happy_server()

    session, _ = await start_session(server, settings)

    subscribe = server.last_of("ProtoOASubscribeSpotsReq").message
    assert sorted(subscribe.symbolId) == [1, 2]
    assert subscribe.subscribeToSpotTimestamp is True
    await session.close()


async def test_catalog_is_published_after_the_handshake(settings: Settings) -> None:
    server = happy_server()

    session, _ = await start_session(server, settings)

    assert session.catalog is not None
    assert session.catalog.names() == ("EURUSD", "XAUUSD")
    assert session.catalog.digits_for("XAUUSD") == 2
    await session.close()


async def test_unresolvable_symbol_fails_closed_and_never_becomes_ready(
    tmp_path: Path,
) -> None:
    """A symbol the broker does not expose is a config error, not a transient one."""
    settings = build_settings(
        tmp_path,
        SYMBOLS="EURUSD,Volatility 75 Index",
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        RECONNECT_MAX_BACKOFF_SECONDS=0.02,
    )
    server = happy_server(symbols=("EURUSD",))

    session, _ = await start_session(server, settings)

    assert not session.is_ready
    assert server.sent_of("ProtoOASubscribeSpotsReq") == []
    await session.close()


# --- spot events -------------------------------------------------------------


async def test_spot_events_reach_the_hub_with_the_right_symbol_and_scaling(
    settings: Settings,
) -> None:
    server = happy_server()
    session, hub = await start_session(server, settings)

    server.push(
        ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=1, bid=108532, ask=108545)
    )
    await asyncio.sleep(0.05)

    tick = hub.last_tick("EURUSD")
    assert tick is not None
    assert (tick.bid, tick.ask) == (1.08532, 1.08545)
    await session.close()


async def test_spot_event_for_an_unknown_symbol_id_is_ignored(settings: Settings) -> None:
    server = happy_server()
    session, hub = await start_session(server, settings)

    server.push(ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=999, bid=1))
    await asyncio.sleep(0.05)

    assert hub.known_symbols() == frozenset()
    assert session.is_ready
    await session.close()


async def test_partial_spot_updates_merge_across_events(settings: Settings) -> None:
    server = happy_server()
    session, hub = await start_session(server, settings)

    server.push(
        ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=1, bid=108532, ask=108545)
    )
    await asyncio.sleep(0.02)
    server.push(ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=1, bid=108540))
    await asyncio.sleep(0.05)

    tick = hub.last_tick("EURUSD")
    assert tick is not None
    assert (tick.bid, tick.ask) == (1.08540, 1.08545)
    await session.close()


# --- reconnect ---------------------------------------------------------------


async def test_reconnect_replays_the_handshake_and_resubscribes(settings: Settings) -> None:
    server = happy_server()
    session, hub = await start_session(server, settings)
    first_subscribe = server.last_of("ProtoOASubscribeSpotsReq").message

    server.drop()
    await asyncio.sleep(0.2)

    assert server.connections >= 2
    assert len(server.sent_of("ProtoOAApplicationAuthReq")) >= 2
    resubscribe = server.last_of("ProtoOASubscribeSpotsReq").message
    assert sorted(resubscribe.symbolId) == sorted(first_subscribe.symbolId)
    assert session.reconnects >= 1
    await session.close()


async def test_stream_subscribers_survive_a_reconnect(settings: Settings) -> None:
    """Subscribers belong to the hub, not the connection."""
    server = happy_server()
    hub = MarketDataHub(queue_size=32)
    subscriber = hub.register()
    session, _ = await start_session(server, settings, hub)

    server.drop()
    await asyncio.sleep(0.2)

    assert hub.subscriber_count == 1
    states = [event.payload["state"] for event in _drain(subscriber) if event.name == "status"]
    assert "reconnecting" in states
    await session.close()


async def test_close_during_backoff_exits_promptly(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=30,
        RECONNECT_MAX_BACKOFF_SECONDS=60,
        REQUEST_TIMEOUT_SECONDS=1,
    )
    server = FakeCTraderServer()
    server.silence("ProtoOAApplicationAuthReq")
    hub = MarketDataHub(queue_size=8)
    session = CTraderSession(settings, hub, connector=server.connector())  # type: ignore[arg-type]
    await session.start()
    await asyncio.sleep(0.05)

    await asyncio.wait_for(session.close(), timeout=2.0)

    assert not session.is_ready


# --- backoff schedule --------------------------------------------------------
#
# The reconnect tests above pin the backoff to 0.01s so they run fast, which
# means the growth curve itself was never observed. These drive _supervise's
# sleeps through a recording stub instead of waiting them out.


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every supervisor backoff sleep without actually waiting."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(CTraderSession, "_sleep_backoff", staticmethod(fake_sleep))
    return delays


async def test_backoff_grows_and_is_capped_including_jitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=1,
        RECONNECT_MAX_BACKOFF_SECONDS=8,
        REQUEST_TIMEOUT_SECONDS=1,
    )
    delays = _record_sleeps(monkeypatch)
    server = FakeCTraderServer()
    # Never completes the handshake, so every attempt fails and backs off.
    server.respond(
        "ProtoOAApplicationAuthReq",
        lambda _request, _mid: ProtoOAErrorRes(errorCode="CH_CLIENT_AUTH_FAILURE"),
    )
    session = CTraderSession(
        settings,
        MarketDataHub(queue_size=8),
        connector=server.connector(),  # type: ignore[arg-type]
    )
    await session.start()
    await asyncio.sleep(0)
    for _ in range(200):
        if len(delays) >= 6:
            break
        await asyncio.sleep(0)
    await session.close()

    assert len(delays) >= 6
    # Jitter is applied inside the cap, so no sleep may exceed the ceiling.
    # Applying it afterwards used to allow 1.5x the configured maximum.
    assert max(delays) <= settings.reconnect_max_backoff_seconds
    # And it does grow: the later half must out-scale the first attempt.
    assert max(delays[3:]) > delays[0]


async def test_a_connection_that_drops_immediately_does_not_reset_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker that accepts auth then hangs up used to reset the backoff on
    every attempt, producing a permanent ~1s hot reconnect loop."""
    settings = build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=1,
        RECONNECT_MAX_BACKOFF_SECONDS=32,
        RECONNECT_STABILITY_SECONDS=30,
        REQUEST_TIMEOUT_SECONDS=1,
    )
    delays = _record_sleeps(monkeypatch)
    server = happy_server()
    session = CTraderSession(
        settings,
        MarketDataHub(queue_size=8),
        connector=server.connector(),  # type: ignore[arg-type]
    )
    await session.start()

    for _ in range(400):
        if len(delays) >= 4:
            break
        if session.is_ready:
            server.drop()
        await asyncio.sleep(0)
    await session.close()

    assert len(delays) >= 4
    assert max(delays[2:]) > delays[0]


# --- token refresh -----------------------------------------------------------


def _expired_then_ok(server: FakeCTraderServer) -> list[str]:
    """Reject the first account auth as expired, accept the second."""
    seen: list[str] = []

    def responder(request, _client_msg_id):
        seen.append(request.accessToken)
        if len(seen) == 1:
            return ProtoOAErrorRes(errorCode="CH_ACCESS_TOKEN_INVALID", description="expired")
        return ProtoOAAccountAuthRes(ctidTraderAccountId=ACCOUNT_ID)

    server.respond("ProtoOAAccountAuthReq", responder)
    return seen


async def test_expired_token_triggers_one_refresh_and_one_retry(settings: Settings) -> None:
    server = happy_server()
    tokens_used = _expired_then_ok(server)
    server.reply_with(
        "ProtoOARefreshTokenReq",
        ProtoOARefreshTokenRes(
            accessToken="rotated-access",
            tokenType="bearer",
            expiresIn=3600,
            refreshToken="rotated-refresh",
        ),
    )

    session, _ = await start_session(server, settings)

    assert len(server.sent_of("ProtoOARefreshTokenReq")) == 1
    assert tokens_used == ["test-access-token", "rotated-access"]
    assert session.is_ready
    await session.close()


async def test_rotated_pair_is_persisted(settings: Settings) -> None:
    """The old refresh token is dead; losing the new one means manual OAuth."""
    server = happy_server()
    _expired_then_ok(server)
    server.reply_with(
        "ProtoOARefreshTokenReq",
        ProtoOARefreshTokenRes(
            accessToken="rotated-access",
            tokenType="bearer",
            expiresIn=3600,
            refreshToken="rotated-refresh",
        ),
    )

    session, _ = await start_session(server, settings)

    cached = json.loads(settings.token_cache_path.read_text())
    assert cached["access_token"] == "rotated-access"
    assert cached["refresh_token"] == "rotated-refresh"
    assert datetime.fromisoformat(cached["expires_at"]) > datetime.now(UTC) + timedelta(minutes=50)
    await session.close()


async def test_a_second_token_failure_does_not_loop(settings: Settings) -> None:
    """Without the one-retry cap this hammers the broker."""
    server = happy_server()
    server.reply_with("ProtoOAAccountAuthReq", ProtoOAErrorRes(errorCode="CH_ACCESS_TOKEN_INVALID"))
    server.reply_with(
        "ProtoOARefreshTokenReq",
        ProtoOARefreshTokenRes(
            accessToken="a", tokenType="bearer", expiresIn=3600, refreshToken="b"
        ),
    )

    session, _ = await start_session(server, settings)
    await asyncio.sleep(0.1)
    refreshes_per_attempt = len(server.sent_of("ProtoOARefreshTokenReq")) / max(
        1, len(server.sent_of("ProtoOAApplicationAuthReq"))
    )

    assert not session.is_ready
    assert refreshes_per_attempt <= 1.0
    await session.close()


async def test_non_token_auth_error_is_not_retried_with_a_refresh(settings: Settings) -> None:
    server = happy_server()
    server.reply_with(
        "ProtoOAAccountAuthReq", ProtoOAErrorRes(errorCode="CH_CTID_TRADER_ACCOUNT_NOT_FOUND")
    )

    session, _ = await start_session(server, settings)

    assert server.sent_of("ProtoOARefreshTokenReq") == []
    assert not session.is_ready
    await session.close()


async def test_proactive_refresher_rotates_before_expiry(settings: Settings) -> None:
    """The 80%-of-lifetime timer had no test driving it — only the arithmetic in
    seconds_until_refresh() was covered, in isolation."""
    server = happy_server()
    _expired_then_ok(server)
    lifetimes = iter([1, 3600])
    server.respond(
        "ProtoOARefreshTokenReq",
        lambda _request, _mid: ProtoOARefreshTokenRes(
            accessToken="rotated-access",
            tokenType="bearer",
            expiresIn=next(lifetimes, 3600),
            refreshToken="rotated-refresh",
        ),
    )
    session, _ = await start_session(server, settings)
    # The reactive refresh above set a 1s lifetime, so the loop is due at ~0.8s.
    assert len(server.sent_of("ProtoOARefreshTokenReq")) == 1

    await asyncio.sleep(1.3)

    assert len(server.sent_of("ProtoOARefreshTokenReq")) >= 2
    await session.close()


# --- unsolicited broker events -----------------------------------------------


async def test_token_invalidated_event_forces_a_refresh_on_reconnect(
    settings: Settings,
) -> None:
    """Closing the socket alone is not enough: the supervisor would reconnect and
    present the same dead token, recovering only if the broker happened to answer
    with one of TOKEN_ERROR_CODES."""
    server = happy_server()
    tokens_used: list[str] = []

    def record_auth(request, _mid):  # type: ignore[no-untyped-def]
        tokens_used.append(request.accessToken)
        return ProtoOAAccountAuthRes(ctidTraderAccountId=ACCOUNT_ID)

    server.respond("ProtoOAAccountAuthReq", record_auth)
    server.reply_with(
        "ProtoOARefreshTokenReq",
        ProtoOARefreshTokenRes(
            accessToken="rotated-access",
            tokenType="bearer",
            expiresIn=3600,
            refreshToken="rotated-refresh",
        ),
    )
    session, _ = await start_session(server, settings)
    assert tokens_used == ["test-access-token"]

    server.push(ProtoOAAccountsTokenInvalidatedEvent(ctidTraderAccountIds=[ACCOUNT_ID], reason="x"))
    await asyncio.sleep(0.3)

    assert len(server.sent_of("ProtoOARefreshTokenReq")) >= 1
    assert tokens_used[-1] == "rotated-access"
    await session.close()


async def test_client_disconnect_event_triggers_a_reconnect(settings: Settings) -> None:
    """ProtoOAClientDisconnectEvent used to match no branch at all — no log, no
    reconnect. wait_closed only fires once the socket actually goes away, which
    on a broker-initiated logout may be much later."""
    server = happy_server()
    session, _ = await start_session(server, settings)
    connections_before = server.connections

    server.push(ProtoOAClientDisconnectEvent(reason="maintenance"))
    await asyncio.sleep(0.3)

    assert server.connections > connections_before
    await session.close()


async def test_supervisor_publishes_the_failure_reason(tmp_path: Path) -> None:
    """`error` was accepted by publish_status but never passed by any caller, so
    /health/ready and the SSE status event never carried a reason."""
    settings = build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        RECONNECT_MAX_BACKOFF_SECONDS=0.02,
        REQUEST_TIMEOUT_SECONDS=1,
    )
    server = FakeCTraderServer()
    server.respond(
        "ProtoOAApplicationAuthReq",
        lambda _request, _mid: ProtoOAErrorRes(errorCode="CH_CLIENT_AUTH_FAILURE"),
    )
    hub = MarketDataHub(queue_size=8)
    session = CTraderSession(settings, hub, connector=server.connector())  # type: ignore[arg-type]
    await session.start()
    await asyncio.sleep(0.2)

    last_error = hub.snapshot(datetime.now(UTC))["last_error"]
    assert last_error is not None
    assert "CH_CLIENT_AUTH_FAILURE" in last_error
    await session.close()


# --- historical --------------------------------------------------------------


async def test_fetch_candles_returns_closed_bars(settings: Settings) -> None:
    server = happy_server()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    bars = [
        ProtoOATrendbar(
            volume=10,
            low=110000,
            deltaOpen=200,
            deltaHigh=500,
            deltaClose=100,
            utcTimestampInMinutes=int((now - timedelta(hours=h)).timestamp() // 60),
        )
        for h in (3, 2)
    ]
    server.reply_with(
        "ProtoOAGetTrendbarsReq",
        ProtoOAGetTrendbarsRes(
            ctidTraderAccountId=ACCOUNT_ID, period=9, symbolId=1, trendbar=bars, hasMore=False
        ),
    )
    session, _ = await start_session(server, settings)

    candles = await session.fetch_candles(symbol="EURUSD", timeframe=Timeframe.H1, count=10)

    assert len(candles) == 2
    assert [c.ts for c in candles] == sorted(c.ts for c in candles)
    assert candles[0].open == 1.102
    assert candles[0].source_instrument == "EURUSD"
    await session.close()


async def test_fetch_candles_paginates_backwards_on_has_more(settings: Settings) -> None:
    """Both existing candle tests set hasMore=False, so the backward walk, the
    infinite-loop guard and the 5 req/s throttle never ran outside the
    integration suite — which has never been executed."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # Each page is strictly older than the last, as the broker returns them when
    # the request walks `toTimestamp` backwards.
    pages: list[list[int]] = [[2, 1], [4, 3], [5]]
    requested_ends: list[int] = []

    def respond(request, _mid):  # type: ignore[no-untyped-def]
        requested_ends.append(int(request.toTimestamp))
        offsets = pages[min(len(requested_ends) - 1, len(pages) - 1)]
        return ProtoOAGetTrendbarsRes(
            ctidTraderAccountId=ACCOUNT_ID,
            period=9,
            symbolId=1,
            trendbar=[
                ProtoOATrendbar(
                    volume=10,
                    low=110000,
                    deltaOpen=200,
                    deltaHigh=500,
                    deltaClose=100,
                    utcTimestampInMinutes=int((now - timedelta(hours=h)).timestamp() // 60),
                )
                for h in offsets
            ],
            hasMore=len(requested_ends) < len(pages),
        )

    server = happy_server()
    server.respond("ProtoOAGetTrendbarsReq", respond)
    session, _ = await start_session(server, settings)

    candles = await session.fetch_candles(symbol="EURUSD", timeframe=Timeframe.H1, count=5)

    assert len(requested_ends) == 3, "should have followed hasMore across all pages"
    # Each page asks for an older window than the last.
    assert requested_ends == sorted(requested_ends, reverse=True)
    assert len(candles) == 5
    assert [c.ts for c in candles] == sorted(c.ts for c in candles)
    assert len({c.ts for c in candles}) == 5, "pages must be de-duplicated by timestamp"
    await session.close()


async def test_fetch_candles_stops_when_a_page_repeats_the_same_window(
    settings: Settings,
) -> None:
    """The guard against a broker that keeps saying hasMore without moving."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    calls = 0

    def respond(_request, _mid):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return ProtoOAGetTrendbarsRes(
            ctidTraderAccountId=ACCOUNT_ID,
            period=9,
            symbolId=1,
            trendbar=[
                ProtoOATrendbar(
                    volume=10,
                    low=110000,
                    deltaOpen=200,
                    deltaHigh=500,
                    deltaClose=100,
                    utcTimestampInMinutes=int((now - timedelta(hours=1)).timestamp() // 60),
                )
            ],
            hasMore=True,
        )

    server = happy_server()
    server.respond("ProtoOAGetTrendbarsReq", respond)
    session, _ = await start_session(server, settings)

    candles = await asyncio.wait_for(
        session.fetch_candles(symbol="EURUSD", timeframe=Timeframe.H1, count=50),
        timeout=5.0,
    )

    assert len(candles) == 1
    assert calls < 50, "must not loop forever on a broker that never advances"
    await session.close()


async def test_fetch_candles_requires_a_ready_session(settings: Settings) -> None:
    from execution_service.errors import CTraderError

    server = FakeCTraderServer()
    server.silence("ProtoOAApplicationAuthReq")
    hub = MarketDataHub(queue_size=8)
    session = CTraderSession(settings, hub, connector=server.connector())  # type: ignore[arg-type]
    await session.start()

    with pytest.raises(CTraderError, match="NOT_CONNECTED"):
        await session.fetch_candles(symbol="EURUSD", timeframe=Timeframe.H1, count=10)

    await session.close()


def _drain(subscriber) -> list:
    events = []
    while not subscriber.queue.empty():
        events.append(subscriber.queue.get_nowait())
    return events
