"""Tests for the bootstrap CLI.

`discover.py` had no coverage at all, which mattered because these are the
commands the README tells an operator to run *first* — before the service can
start — to obtain CTRADER_ACCOUNT_ID and SYMBOLS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_service import discover
from execution_service.config import Settings
from execution_service.ctrader.proto import (
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthRes,
    ProtoOACtidTraderAccount,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOALightSymbol,
    ProtoOARefreshTokenRes,
    ProtoOASymbol,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListRes,
)
from tests.conftest import build_settings
from tests.fakes import FakeCTraderServer

ACCOUNT_ID = 12345678


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path, REQUEST_TIMEOUT_SECONDS=1)


def _authenticated_server() -> FakeCTraderServer:
    server = FakeCTraderServer()
    server.reply_with("ProtoOAApplicationAuthReq", ProtoOAApplicationAuthRes())
    server.reply_with(
        "ProtoOAAccountAuthReq", ProtoOAAccountAuthRes(ctidTraderAccountId=ACCOUNT_ID)
    )
    return server


# --- discover_accounts -------------------------------------------------------


async def test_discover_accounts_prints_every_reachable_account(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _authenticated_server()
    server.reply_with(
        "ProtoOAGetAccountListByAccessTokenReq",
        ProtoOAGetAccountListByAccessTokenRes(
            accessToken="test-access-token",
            ctidTraderAccount=[
                ProtoOACtidTraderAccount(
                    ctidTraderAccountId=ACCOUNT_ID, isLive=False, traderLogin=999
                ),
                ProtoOACtidTraderAccount(
                    ctidTraderAccountId=87654321, isLive=True, traderLogin=111
                ),
            ],
        ),
    )

    code = await discover.discover_accounts(settings, server.connector())  # type: ignore[arg-type]

    assert code == 0
    output = capsys.readouterr().out
    assert str(ACCOUNT_ID) in output
    assert "87654321" in output
    # Account auth is deliberately skipped: the account id is what is missing.
    assert "ProtoOAAccountAuthReq" not in server.sent_names()


async def test_discover_accounts_reports_an_empty_result(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _authenticated_server()
    server.reply_with(
        "ProtoOAGetAccountListByAccessTokenReq",
        ProtoOAGetAccountListByAccessTokenRes(accessToken="test-access-token"),
    )

    code = await discover.discover_accounts(settings, server.connector())  # type: ignore[arg-type]

    assert code == 1
    assert "No trading accounts" in capsys.readouterr().err


async def test_a_single_account_does_not_break_column_sizing(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """_print_table star-unpacks the row widths, which is the shape most likely
    to fail on exactly one row."""
    server = _authenticated_server()
    server.reply_with(
        "ProtoOAGetAccountListByAccessTokenReq",
        ProtoOAGetAccountListByAccessTokenRes(
            accessToken="test-access-token",
            ctidTraderAccount=[
                ProtoOACtidTraderAccount(ctidTraderAccountId=7, isLive=False, traderLogin=1)
            ],
        ),
    )

    code = await discover.discover_accounts(settings, server.connector())  # type: ignore[arg-type]

    assert code == 0
    assert "ctidTraderAccountId" in capsys.readouterr().out


# --- discover_symbols --------------------------------------------------------


async def test_discover_symbols_prints_names_and_digits(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _authenticated_server()
    server.reply_with(
        "ProtoOASymbolsListReq",
        ProtoOASymbolsListRes(
            ctidTraderAccountId=ACCOUNT_ID,
            symbol=[
                ProtoOALightSymbol(symbolId=1, symbolName="EURUSD", enabled=True),
                ProtoOALightSymbol(symbolId=2, symbolName="XAUUSD", enabled=False),
            ],
        ),
    )
    server.reply_with(
        "ProtoOASymbolByIdReq",
        ProtoOASymbolByIdRes(
            ctidTraderAccountId=ACCOUNT_ID,
            symbol=[
                ProtoOASymbol(symbolId=1, digits=5, pipPosition=4),
                ProtoOASymbol(symbolId=2, digits=2, pipPosition=1),
            ],
        ),
    )

    code = await discover.discover_symbols(settings, server.connector())  # type: ignore[arg-type]

    assert code == 0
    output = capsys.readouterr().out
    assert "EURUSD" in output
    assert "XAUUSD" in output
    assert "2 symbols" in output


async def test_discover_symbols_reports_an_empty_catalog(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _authenticated_server()
    server.reply_with(
        "ProtoOASymbolsListReq", ProtoOASymbolsListRes(ctidTraderAccountId=ACCOUNT_ID)
    )

    code = await discover.discover_symbols(settings, server.connector())  # type: ignore[arg-type]

    assert code == 1
    assert "no symbols" in capsys.readouterr().err


# --- refresh_token -----------------------------------------------------------


async def test_refresh_token_persists_the_rotated_pair(
    tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _authenticated_server()
    server.reply_with(
        "ProtoOARefreshTokenReq",
        ProtoOARefreshTokenRes(
            accessToken="rotated-access",
            tokenType="bearer",
            refreshToken="rotated-refresh",
            expiresIn=3600,
        ),
    )

    code = await discover.refresh_token(settings, server.connector())  # type: ignore[arg-type]

    assert code == 0
    cached = json.loads(settings.token_cache_path.read_text(encoding="utf-8"))
    assert cached["access_token"] == "rotated-access"
    # The old refresh token dies on use, so persisting the new one is the whole
    # point of the command.
    assert cached["refresh_token"] == "rotated-refresh"
    assert "Refreshed" in capsys.readouterr().out


async def test_refresh_token_without_one_configured_is_a_clean_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = build_settings(tmp_path, CTRADER_REFRESH_TOKEN=None)
    server = _authenticated_server()

    code = await discover.refresh_token(settings, server.connector())  # type: ignore[arg-type]

    assert code == 1
    assert "CTRADER_REFRESH_TOKEN" in capsys.readouterr().err


# --- broker failures ---------------------------------------------------------


async def test_broker_error_is_reported_not_raised(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A traceback here reads as a bug in the tool rather than as bad credentials."""
    server = FakeCTraderServer()
    server.respond(
        "ProtoOAApplicationAuthReq",
        lambda _request, _mid: ProtoOAErrorRes(
            errorCode="CH_CLIENT_AUTH_FAILURE", description="bad client id"
        ),
    )

    code = await discover.discover_accounts(settings, server.connector())  # type: ignore[arg-type]

    assert code == 1
    stderr = capsys.readouterr().err
    assert "CH_CLIENT_AUTH_FAILURE" in stderr
    assert "Traceback" not in stderr


async def test_timeout_is_reported_not_raised(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeCTraderServer()
    server.silence("ProtoOAApplicationAuthReq")

    code = await discover.discover_symbols(settings, server.connector())  # type: ignore[arg-type]

    assert code == 1
    assert "did not respond in time" in capsys.readouterr().err
