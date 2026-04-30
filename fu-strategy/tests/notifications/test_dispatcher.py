from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.core.types import Bias, Direction, Signal
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog
from app.notifications.sms_client import SMSError
from app.notifications.whatsapp_client import WhatsAppError


def _signal() -> Signal:
    return Signal(
        id='signal-1',
        symbol='EUR_USD',
        timeframe='15M',
        direction=Direction.BUY,
        entry_price=1.1,
        sl=1.09,
        tp=1.12,
        structure_bias=Bias.BULLISH,
        fu_candle_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        confidence=['fu_only_mode'],
    )


async def _dispatcher(tmp_path, *, whatsapp=True, sms=True,
                      notification_channels=None):
    settings = Settings(
        _env_file=None,
        notification_numbers=['+111', '+222'],
        notification_channels=notification_channels or ['whatsapp', 'sms'],
        whatsapp_access_token='wa-token' if whatsapp else None,
        whatsapp_phone_number_id='phone-id' if whatsapp else None,
        pindo_token='sms-token' if sms else None,
    )
    log = NotificationLog(str(tmp_path / 'notifications.jsonl'))
    await log.init()
    whatsapp_client = AsyncMock()
    whatsapp_client.send_text.return_value = {'messages': [{'id': 'wamid-1'}]}
    sms_client = AsyncMock()
    sms_client.send_text.return_value = {'id': 'sms-1'}
    dispatcher = NotificationDispatcher(settings, whatsapp_client, sms_client, log)
    return dispatcher, log, whatsapp_client, sms_client


@pytest.mark.asyncio
async def test_notify_signal_logs_whatsapp_and_sms_per_recipient(tmp_path):
    dispatcher, log, whatsapp_client, sms_client = await _dispatcher(tmp_path)

    log_ids = await dispatcher.notify_signal(_signal())
    records = await log.list_recent(limit=10)

    assert len(log_ids) == 4
    assert sorted(r.channel for r in records) == ['sms', 'sms', 'whatsapp', 'whatsapp']
    assert all(r.status == 'sent' for r in records)
    sms_records = [r for r in records if r.channel == 'sms']
    assert all(len(r.body) <= 150 for r in sms_records)
    assert all('Confluence' not in r.body for r in sms_records)
    assert whatsapp_client.send_text.await_count == 2
    assert sms_client.send_text.await_count == 2


@pytest.mark.asyncio
async def test_notify_signal_skips_sms_when_unconfigured(tmp_path):
    dispatcher, log, whatsapp_client, sms_client = await _dispatcher(tmp_path, sms=False)

    log_ids = await dispatcher.notify_signal(_signal())
    records = await log.list_recent(limit=10)

    assert len(log_ids) == 2
    assert [r.channel for r in records] == ['whatsapp', 'whatsapp']
    assert whatsapp_client.send_text.await_count == 2
    sms_client.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_signal_respects_sms_only_channel_allow_list(tmp_path):
    dispatcher, log, whatsapp_client, sms_client = await _dispatcher(
        tmp_path,
        whatsapp=True,
        sms=True,
        notification_channels=['sms'],
    )

    log_ids = await dispatcher.notify_signal(_signal())
    records = await log.list_recent(limit=10)

    assert len(log_ids) == 2
    assert [r.channel for r in records] == ['sms', 'sms']
    whatsapp_client.send_text.assert_not_awaited()
    assert sms_client.send_text.await_count == 2


@pytest.mark.asyncio
async def test_whatsapp_failure_does_not_prevent_sms(tmp_path):
    dispatcher, log, whatsapp_client, sms_client = await _dispatcher(tmp_path)
    whatsapp_client.send_text.side_effect = WhatsAppError(
        'wa failed', response_body={'error': 'bad'},
    )

    log_ids = await dispatcher.notify_signal(_signal())
    records = await log.list_recent(limit=10)
    by_channel = {r.channel: [] for r in records}
    for record in records:
        by_channel[record.channel].append(record.status)

    assert len(log_ids) == 4
    assert sorted(by_channel['whatsapp']) == ['failed', 'failed']
    assert sorted(by_channel['sms']) == ['sent', 'sent']
    assert sms_client.send_text.await_count == 2


@pytest.mark.asyncio
async def test_sms_failure_marks_only_sms_failed(tmp_path):
    dispatcher, log, whatsapp_client, sms_client = await _dispatcher(tmp_path)
    sms_client.send_text.side_effect = SMSError('sms failed', response_body={'error': 'bad'})

    log_ids = await dispatcher.notify_signal(_signal())
    records = await log.list_recent(limit=10)
    by_channel = {r.channel: [] for r in records}
    for record in records:
        by_channel[record.channel].append(record.status)

    assert len(log_ids) == 4
    assert sorted(by_channel['whatsapp']) == ['sent', 'sent']
    assert sorted(by_channel['sms']) == ['failed', 'failed']
    assert whatsapp_client.send_text.await_count == 2
