"""POST /notifications/test/broadcast"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.notifications import build_router


def _app_with_dispatcher(**settings_kw):
    mock_log = MagicMock()
    dispatcher = MagicMock()
    dispatcher.settings = MagicMock(**settings_kw)
    dispatcher.send_test = AsyncMock(side_effect=['log-a', 'log-b'])
    app = FastAPI()
    app.include_router(build_router(mock_log, dispatcher))
    return app, dispatcher


@pytest.mark.asyncio
async def test_broadcast_success():
    app, dispatcher = _app_with_dispatcher(
        whatsapp_configured=True,
        notification_numbers=['+111', '+222'],
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/notifications/test/broadcast',
            json={'message': 'hello'},
        )
    assert r.status_code == 200
    assert r.json() == {
        'log_ids': ['log-a', 'log-b'],
        'recipients_attempted': 2,
    }
    assert dispatcher.send_test.await_count == 2
    calls = [c.kwargs for c in dispatcher.send_test.await_args_list]
    assert calls == [
        {'recipient': '+111', 'body': 'hello'},
        {'recipient': '+222', 'body': 'hello'},
    ]


@pytest.mark.asyncio
async def test_broadcast_empty_recipients():
    app, _ = _app_with_dispatcher(
        whatsapp_configured=True,
        notification_numbers=[],
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/notifications/test/broadcast',
            json={'message': 'hello'},
        )
    assert r.status_code == 400
    assert 'NOTIFICATION_NUMBERS' in r.json()['detail']


@pytest.mark.asyncio
async def test_broadcast_whatsapp_not_configured():
    app, _ = _app_with_dispatcher(
        whatsapp_configured=False,
        notification_numbers=['+1'],
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.post(
            '/notifications/test/broadcast',
            json={'message': 'hello'},
        )
    assert r.status_code == 503
    assert 'WhatsApp not configured' in r.json()['detail']
