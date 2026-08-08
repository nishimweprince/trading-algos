from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import Settings
from ctrader.proto import (
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetTrendbarsRes,
    ProtoOALightSymbol,
    ProtoOARefreshTokenRes,
    ProtoOASpotEvent,
    ProtoOASubscribeLiveTrendbarRes,
    ProtoOASubscribeSpotsRes,
    ProtoOASymbol,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListRes,
    ProtoOATrendbar,
)
from ctrader.session import CTraderSession
from hub import MarketDataHub
from models import Timeframe
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
    server.reply_with(
        "ProtoOASubscribeLiveTrendbarReq",
        ProtoOASubscribeLiveTrendbarRes(ctidTraderAccountId=ACCOUNT_ID),
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
        LIVE_TRENDBAR_PERIODS="",
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


async def test_live_trendbar_subscription_per_symbol_and_period(
    tmp_path: Path,
) -> None:
    settings = build_settings(
        tmp_path, LIVE_TRENDBAR_PERIODS="M1,M5", RECONNECT_INITIAL_BACKOFF_SECONDS=0.01
    )
    server = happy_server()

    session, _ = await start_session(server, settings)

    assert len(server.sent_of("ProtoOASubscribeLiveTrendbarReq")) == 4  # 2 symbols x 2 periods
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


async def test_fetch_candles_requires_a_ready_session(settings: Settings) -> None:
    from errors import CTraderError

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
