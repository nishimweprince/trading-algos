import json

import pytest

from app.notifications.log import NotificationLog


@pytest.mark.asyncio
async def test_notification_log_defaults_legacy_records_to_whatsapp(tmp_path):
    path = tmp_path / 'notifications.jsonl'
    path.write_text(json.dumps({
        'id': 'legacy',
        'signal_id': None,
        'recipient': '+111',
        'message_type': 'text',
        'body': 'hello',
        'status': 'sent',
        'wamid': 'wamid-1',
        'error': None,
        'created_at': '2024-01-01T00:00:00',
        'sent_at': '2024-01-01T00:00:01',
        'updated_at': '2024-01-01T00:00:01',
    }) + '\n')

    log = NotificationLog(str(path))
    records = await log.list_recent()

    assert len(records) == 1
    assert records[0].channel == 'whatsapp'
