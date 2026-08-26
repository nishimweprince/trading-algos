"""One-shot CLI helpers for filling in an .env file.

Both connect, ask one question, print a table, and exit. They exist because the
two values an operator cannot guess — ctidTraderAccountId and the exact broker
symbol names — are only obtainable over the authenticated protobuf channel.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable

from .adapters.ctrader.proto import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOARefreshTokenReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
)
from .adapters.ctrader.protocol import Connector, CTraderProtocolClient, tls_connector
from .adapters.ctrader.tokens import TokenPair, TokenStore
from .config import Settings
from .errors import CTraderError, CTraderTimeout


async def _reporting_broker_errors(action: Callable[[], Awaitable[int]]) -> int:
    """Turn a broker failure into the same stderr-plus-exit-code shape as the
    expected empty results, instead of an unhandled traceback out of asyncio.run.

    These are operator-facing bootstrap commands; a traceback here reads as a bug
    in the tool rather than as "your credentials are wrong".
    """
    try:
        return await action()
    except CTraderTimeout as exc:
        print(f"The broker did not respond in time: {exc}", file=sys.stderr)
        return 1
    except CTraderError as exc:
        print(f"The broker rejected the request [{exc.error_code}]: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not reach the broker: {exc}", file=sys.stderr)
        return 1


def _build_client(settings: Settings, connector: Connector | None) -> CTraderProtocolClient:
    return CTraderProtocolClient(
        connector or tls_connector(settings.resolved_host, settings.ctrader_port),
        on_event=lambda _message: None,
        request_timeout=settings.request_timeout_seconds,
        heartbeat_interval=settings.heartbeat_interval_seconds,
        connect_timeout=settings.connect_timeout_seconds,
    )


def _token_store(settings: Settings) -> TokenStore:
    store = TokenStore(
        settings.token_cache_path,
        fallback=TokenPair(
            access_token=settings.access_token.get_secret_value(),
            refresh_token=(
                settings.refresh_token.get_secret_value() if settings.refresh_token else None
            ),
        ),
    )
    store.load()
    return store


async def _authenticate_application(client: CTraderProtocolClient, settings: Settings) -> None:
    await client.request(
        ProtoOAApplicationAuthReq(
            clientId=settings.client_id.get_secret_value(),
            clientSecret=settings.client_secret.get_secret_value(),
        )
    )


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


async def discover_accounts(settings: Settings, connector: Connector | None = None) -> int:
    """List every ctidTraderAccountId reachable with the access token.

    Needs only application auth plus the token — deliberately not account auth,
    since the account id is exactly what the operator is missing.
    """
    return await _reporting_broker_errors(lambda: _discover_accounts(settings, connector))


async def _discover_accounts(settings: Settings, connector: Connector | None) -> int:
    client = _build_client(settings, connector)
    try:
        await client.connect()
        await _authenticate_application(client, settings)
        response = await client.request(
            ProtoOAGetAccountListByAccessTokenReq(
                accessToken=_token_store(settings).current.access_token
            )
        )
        accounts = list(response.ctidTraderAccount)
        if not accounts:
            print("No trading accounts are linked to this access token.", file=sys.stderr)
            return 1
        _print_table(
            ["ctidTraderAccountId", "live", "traderLogin", "broker"],
            [
                [
                    str(account.ctidTraderAccountId),
                    "yes" if account.isLive else "no",
                    str(account.traderLogin),
                    account.brokerTitleShort or "",
                ]
                for account in accounts
            ],
        )
        print("\nSet CTRADER_ACCOUNT_ID to the id matching your CTRADER_ENVIRONMENT.")
        return 0
    finally:
        await client.close()


async def discover_symbols(settings: Settings, connector: Connector | None = None) -> int:
    """List the broker's symbols so SYMBOLS can be filled with exact names."""
    return await _reporting_broker_errors(lambda: _discover_symbols(settings, connector))


async def _discover_symbols(settings: Settings, connector: Connector | None) -> int:
    client = _build_client(settings, connector)
    try:
        await client.connect()
        await _authenticate_application(client, settings)
        await client.request(
            ProtoOAAccountAuthReq(
                ctidTraderAccountId=settings.account_id,
                accessToken=_token_store(settings).current.access_token,
            )
        )
        listed = await client.request(
            ProtoOASymbolsListReq(
                ctidTraderAccountId=settings.account_id, includeArchivedSymbols=False
            )
        )
        symbols = sorted(listed.symbol, key=lambda s: str(s.symbolName))
        if not symbols:
            print("The broker returned no symbols for this account.", file=sys.stderr)
            return 1

        detailed = await client.request(
            ProtoOASymbolByIdReq(
                ctidTraderAccountId=settings.account_id,
                symbolId=[int(s.symbolId) for s in symbols],
            )
        )
        digits = {int(s.symbolId): int(s.digits) for s in detailed.symbol}

        _print_table(
            ["symbolId", "symbolName", "digits", "enabled"],
            [
                [
                    str(symbol.symbolId),
                    str(symbol.symbolName),
                    str(digits.get(int(symbol.symbolId), "?")),
                    "yes" if symbol.enabled else "no",
                ]
                for symbol in symbols
            ],
        )
        print(f"\n{len(symbols)} symbols. Copy exact symbolName values into SYMBOLS.")
        return 0
    finally:
        await client.close()


async def refresh_token(settings: Settings, connector: Connector | None = None) -> int:
    """Rotate the OAuth token pair and persist it to TOKEN_CACHE_PATH."""
    return await _reporting_broker_errors(lambda: _refresh_token(settings, connector))


async def _refresh_token(settings: Settings, connector: Connector | None) -> int:
    store = _token_store(settings)
    if not store.current.refresh_token:
        print("No refresh token configured. Set CTRADER_REFRESH_TOKEN.", file=sys.stderr)
        return 1

    client = _build_client(settings, connector)
    try:
        await client.connect()
        await _authenticate_application(client, settings)
        response = await client.request(
            ProtoOARefreshTokenReq(refreshToken=store.current.refresh_token)
        )
        pair = store.record_refresh(
            access_token=response.accessToken,
            refresh_token=response.refreshToken,
            expires_in=response.expiresIn,
        )
        expiry = f"{pair.expires_at:%Y-%m-%d %H:%M} UTC"
        print(f"Refreshed. Expires in {int(response.expiresIn)}s ({expiry}).")
        print(f"Rotated pair written to {settings.token_cache_path}.")
        return 0
    finally:
        await client.close()
