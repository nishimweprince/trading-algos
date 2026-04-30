"""Notifications API — history and a manual test ping."""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog


class TestSendRequest(BaseModel):
    recipient: str
    message: str = 'FU Strategy test ping'


class TestSendResponse(BaseModel):
    log_ids: List[str]


class TestBroadcastRequest(BaseModel):
    message: str


class TestBroadcastResponse(BaseModel):
    log_ids: List[str]
    recipients_attempted: int


def build_router(log: NotificationLog, dispatcher: NotificationDispatcher) -> APIRouter:
    router = APIRouter(prefix='/notifications', tags=['notifications'])

    @router.get('')
    async def list_notifications(
        limit: int = Query(100, ge=1, le=1000),
        signal_id: Optional[str] = None,
        recipient: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[dict]:
        records = await log.list_recent(
            limit=limit, signal_id=signal_id, recipient=recipient, status=status,
        )
        return [r.to_dict() for r in records]

    @router.post('/test', response_model=TestSendResponse)
    async def send_test(req: TestSendRequest) -> TestSendResponse:
        log_ids = await dispatcher.send_test(recipient=req.recipient, body=req.message)
        return TestSendResponse(log_ids=list(log_ids))

    @router.post('/test/broadcast', response_model=TestBroadcastResponse)
    async def send_test_broadcast(req: TestBroadcastRequest) -> TestBroadcastResponse:
        """Send the same text to every number in NOTIFICATION_NUMBERS (.env).

        Ops test endpoint: sends even when NOTIFICATIONS_ENABLED is false, as long as
        at least one channel is configured and NOTIFICATION_NUMBERS is set.
        """
        settings = dispatcher.settings
        if not dispatcher._has_sendable_channel():
            raise HTTPException(
                status_code=503,
                detail='No notification channels allowed/configured',
            )
        if not settings.notification_numbers:
            raise HTTPException(
                status_code=400,
                detail='NOTIFICATION_NUMBERS is empty',
            )
        tasks = [
            dispatcher.send_test(recipient=r, body=req.message)
            for r in settings.notification_numbers
        ]
        nested_log_ids = await asyncio.gather(*tasks)
        log_ids = [log_id for recipient_ids in nested_log_ids for log_id in recipient_ids]
        return TestBroadcastResponse(
            log_ids=log_ids,
            recipients_attempted=len(settings.notification_numbers),
        )

    @router.get('/{log_id}')
    async def get_notification(log_id: str) -> dict:
        record = await log.get(log_id)
        if record is None:
            raise HTTPException(status_code=404, detail='Notification not found')
        return record.to_dict()

    return router
