"""Notifications API — history and a manual test ping."""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog


class TestSendRequest(BaseModel):
    recipient: str
    message: str = 'FU Strategy test ping'


class TestSendResponse(BaseModel):
    log_id: str


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

    @router.get('/{log_id}')
    async def get_notification(log_id: str) -> dict:
        record = await log.get(log_id)
        if record is None:
            raise HTTPException(status_code=404, detail='Notification not found')
        return record.to_dict()

    @router.post('/test', response_model=TestSendResponse)
    async def send_test(req: TestSendRequest) -> TestSendResponse:
        log_id = await dispatcher.send_test(recipient=req.recipient, body=req.message)
        return TestSendResponse(log_id=log_id)

    return router
