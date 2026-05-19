"""POST /webhooks/tradingview"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.webhooks import build_router


def _app_with_dispatcher(**settings_kw):
    mock_client = MagicMock()
    mock_log = MagicMock()
    dispatcher = MagicMock()
    dispatcher.settings = MagicMock(**settings_kw)
    dispatcher.broadcast_message = AsyncMock(return_value=['log-1', 'log-2'])

    app = FastAPI()
    app.include_router(build_router(mock_client, mock_log, dispatcher))
    return app, dispatcher


@pytest.mark.asyncio
async def test_tradingview_webhook_no_token_configured():
    app, dispatcher = _app_with_dispatcher(
        tradingview_webhook_token=None,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/webhooks/tradingview',
            json={
                'symbol': 'XAUUSD',
                'timeframe': '1m',
                'signal': 'Reversal Up Chance',
                'price': 4684.62,
            },
        )
    assert r.status_code == 200
    assert r.json() == {'status': 'success', 'log_ids': ['log-1', 'log-2']}

    # Verify the message body formatted correctly with a green circle
    dispatcher.broadcast_message.assert_awaited_once()
    body = dispatcher.broadcast_message.await_args[0][0]
    assert '🟢 [LUXALGO] Reversal Up Chance' in body
    assert '🪙 Symbol: XAUUSD' in body
    assert '⏱️ Timeframe: 1m' in body
    assert '💲 Price: 4684.62' in body


@pytest.mark.asyncio
async def test_tradingview_webhook_with_token_success():
    app, dispatcher = _app_with_dispatcher(
        tradingview_webhook_token='super-secret-token',
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/webhooks/tradingview?token=super-secret-token',
            json={
                'symbol': 'BTCUSD',
                'timeframe': '1m',
                'signal': 'Reversal Down Chance',
                'price': 65000.0,
            },
        )
    assert r.status_code == 200
    assert r.json() == {'status': 'success', 'log_ids': ['log-1', 'log-2']}

    # Verify the message body formatted correctly with a red circle
    dispatcher.broadcast_message.assert_awaited_once()
    body = dispatcher.broadcast_message.await_args[0][0]
    assert '🔴 [LUXALGO] Reversal Down Chance' in body
    assert '🪙 Symbol: BTCUSD' in body
    assert '⏱️ Timeframe: 1m' in body
    assert '💲 Price: 65000' in body


@pytest.mark.asyncio
async def test_tradingview_webhook_with_token_missing():
    app, dispatcher = _app_with_dispatcher(
        tradingview_webhook_token='super-secret-token',
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/webhooks/tradingview',
            json={
                'symbol': 'XAUUSD',
                'timeframe': '1m',
                'signal': 'Reversal Up Chance',
                'price': 4684.62,
            },
        )
    assert r.status_code == 403
    assert r.json()['detail'] == 'Invalid webhook token'
    dispatcher.broadcast_message.assert_not_called()


@pytest.mark.asyncio
async def test_tradingview_webhook_with_token_incorrect():
    app, dispatcher = _app_with_dispatcher(
        tradingview_webhook_token='super-secret-token',
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/webhooks/tradingview?token=wrong-token',
            json={
                'symbol': 'XAUUSD',
                'timeframe': '1m',
                'signal': 'Reversal Up Chance',
                'price': 4684.62,
            },
        )
    assert r.status_code == 403
    assert r.json()['detail'] == 'Invalid webhook token'
    dispatcher.broadcast_message.assert_not_called()


@pytest.mark.asyncio
async def test_tradingview_webhook_other_timeframe_skipped():
    app, dispatcher = _app_with_dispatcher(
        tradingview_webhook_token=None,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/webhooks/tradingview',
            json={
                'symbol': 'XAUUSD',
                'timeframe': '15m',
                'signal': 'Reversal Up Chance',
                'price': 4684.62,
            },
        )
    assert r.status_code == 200
    assert r.json() == {
        'status': 'skipped',
        'reason': 'Only 1-minute timeframe alerts are allowed',
    }
    dispatcher.broadcast_message.assert_not_called()
