from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.notifications.sms_client import SMSClient, SMSError


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {'ok': True}
        self.text = 'plain error'

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_sms_client_sends_text():
    settings = Settings(
        _env_file=None,
        pindo_token='token',
        pindo_api_url='https://sms.test/',
        pindo_sender_id='FU',
    )
    client = SMSClient(settings)
    http = AsyncMock()
    http.post.return_value = FakeResponse(body={'message_id': 'sms-1'})
    client._client = http

    response = await client.send_text(to='+250700000000', text='hello')

    assert response == {'message_id': 'sms-1'}
    http.post.assert_awaited_once_with(
        'https://sms.test/',
        headers={
            'Authorization': 'Bearer token',
            'Content-Type': 'application/json',
        },
        json={
            'to': '+250700000000',
            'text': 'hello',
            'sender': 'FU',
        },
    )


@pytest.mark.asyncio
async def test_sms_client_raises_on_api_error():
    settings = Settings(_env_file=None, pindo_token='token')
    client = SMSClient(settings)
    http = AsyncMock()
    http.post.return_value = FakeResponse(status_code=400, body={'error': 'bad'})
    client._client = http

    with pytest.raises(SMSError) as exc:
        await client.send_text(to='+250700000000', text='hello')

    assert exc.value.status_code == 400
    assert exc.value.response_body == {'error': 'bad'}


@pytest.mark.asyncio
async def test_sms_client_raises_on_transport_error():
    settings = Settings(_env_file=None, pindo_token='token')
    client = SMSClient(settings)
    http = AsyncMock()
    http.post.side_effect = httpx.ConnectError('offline')
    client._client = http

    with pytest.raises(SMSError) as exc:
        await client.send_text(to='+250700000000', text='hello')

    assert 'Transport error' in str(exc.value)
