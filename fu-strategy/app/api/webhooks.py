"""WhatsApp webhook receiver.

GET  /webhooks/whatsapp  — Meta verification handshake.
POST /webhooks/whatsapp  — delivery status events. Validates X-Hub-Signature-256
                            against WHATSAPP_APP_SECRET, then updates the
                            notification log status by `wamid`.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from loguru import logger

from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog
from app.notifications.whatsapp_client import WhatsAppClient


def build_router(client: WhatsAppClient, log: NotificationLog,
                 dispatcher: NotificationDispatcher) -> APIRouter:
    router = APIRouter(prefix='/webhooks', tags=['webhooks'])

    @router.get('/whatsapp', response_class=Response)
    async def verify(
        hub_mode: Optional[str] = Query(None, alias='hub.mode'),
        hub_verify_token: Optional[str] = Query(None, alias='hub.verify_token'),
        hub_challenge: Optional[str] = Query(None, alias='hub.challenge'),
    ):
        challenge = client.verify_webhook_challenge(hub_mode, hub_verify_token, hub_challenge)
        if challenge is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Verification failed')
        return Response(content=challenge, media_type='text/plain', status_code=200)

    @router.post('/whatsapp')
    async def receive(
        request: Request,
        x_hub_signature_256: Optional[str] = Header(default=None, alias='X-Hub-Signature-256'),
    ):
        raw_body = await request.body()

        if not client.verify_signature(raw_body, x_hub_signature_256):
            logger.warning("Rejecting webhook: signature mismatch")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid signature')

        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            logger.warning("Webhook body was not JSON; ignoring")
            return {'ok': True}

        await _process_status_events(payload, log)
        return {'ok': True}

    return router


async def _process_status_events(payload: Dict[str, Any], log: NotificationLog) -> None:
    """Walk the Meta webhook envelope, update logs by wamid."""
    for entry in payload.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            value = change.get('value', {}) or {}
            for status_event in value.get('statuses', []) or []:
                wamid = status_event.get('id')
                new_status = status_event.get('status')
                error = None
                errors = status_event.get('errors')
                if errors:
                    error = '; '.join(
                        f"{e.get('code')}:{e.get('title')}:{e.get('message', '')}"
                        for e in errors if isinstance(e, dict)
                    )
                if wamid and new_status:
                    rows = await log.update_status_by_wamid(wamid, new_status, error)
                    logger.info(f"Webhook status: wamid={wamid} status={new_status} updated={rows}")
