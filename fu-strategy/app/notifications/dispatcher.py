"""Notification dispatcher — fan a Signal out to all configured recipients."""
import asyncio
from typing import List, Optional

from loguru import logger

from app.config import Settings
from app.core.types import Signal
from app.notifications.formatters import format_signal_template_params, format_signal_text
from app.notifications.log import NotificationLog
from app.notifications.whatsapp_client import WhatsAppClient, WhatsAppError


class NotificationDispatcher:
    """Sends signal notifications via WhatsApp and records every attempt."""

    def __init__(self, settings: Settings, client: WhatsAppClient, log: NotificationLog):
        self.settings = settings
        self.client = client
        self.log = log

    async def notify_signal(self, signal: Signal) -> List[str]:
        """Fan out a signal to every recipient. Returns the list of log row ids."""
        if not self.settings.notifications_enabled:
            logger.debug("Notifications disabled; skipping fan-out")
            return []
        if not self.settings.whatsapp_configured:
            logger.warning("WhatsApp not configured; skipping notifications for signal %s", signal.id)
            return []
        if not self.settings.notification_numbers:
            logger.warning("NOTIFICATION_NUMBERS empty; nobody to notify")
            return []

        body = format_signal_text(signal)
        tasks = [
            self._send_one(recipient=r, signal_id=signal.id, body=body, signal=signal)
            for r in self.settings.notification_numbers
        ]
        return await asyncio.gather(*tasks, return_exceptions=False)

    async def send_test(self, recipient: str, body: str) -> str:
        """Send an ad-hoc text message; bypasses signal formatting."""
        return await self._send_one(recipient=recipient, signal_id=None, body=body, signal=None)

    async def _send_one(self, *, recipient: str, signal_id: Optional[str],
                        body: str, signal: Optional[Signal]) -> str:
        use_template = bool(self.settings.whatsapp_template_name) and signal is not None
        message_type = 'template' if use_template else 'text'

        log_id = await self.log.create_pending(
            signal_id=signal_id, recipient=recipient,
            message_type=message_type, body=body,
        )

        try:
            if use_template:
                params = format_signal_template_params(signal)  # type: ignore[arg-type]
                response = await self.client.send_template(
                    to=recipient,
                    template_name=self.settings.whatsapp_template_name,  # type: ignore[arg-type]
                    language_code=self.settings.whatsapp_template_language,
                    body_parameters=params,
                )
            else:
                response = await self.client.send_text(to=recipient, body=body)

            wamid = self._extract_wamid(response)
            await self.log.mark_sent(log_id, wamid=wamid)
            logger.info(f"WhatsApp sent to {recipient} (log_id={log_id}, wamid={wamid})")
        except WhatsAppError as e:
            error = f"{e} | body={e.response_body}"
            await self.log.mark_failed(log_id, error=error)
            logger.error(f"WhatsApp send failed for {recipient} (log_id={log_id}): {error}")
        except Exception as e:
            await self.log.mark_failed(log_id, error=str(e))
            logger.exception(f"Unexpected notification error for {recipient} (log_id={log_id})")

        return log_id

    @staticmethod
    def _extract_wamid(response: dict) -> Optional[str]:
        messages = response.get('messages') if isinstance(response, dict) else None
        if isinstance(messages, list) and messages:
            return messages[0].get('id')
        return None
