"""Notification dispatcher — fan a Signal out to all configured recipients."""
import asyncio
from typing import List, Optional

from loguru import logger

from app.config import Settings
from app.core.types import Signal
from app.notifications.formatters import (
    format_signal_sms_text,
    format_signal_template_params,
    format_signal_text,
)
from app.notifications.log import NotificationLog
from app.notifications.sms_client import SMSClient, SMSError
from app.notifications.whatsapp_client import WhatsAppClient, WhatsAppError


class NotificationDispatcher:
    """Sends signal notifications via configured channels and records every attempt."""

    def __init__(self, settings: Settings, whatsapp_client: WhatsAppClient,
                 sms_client: SMSClient, log: NotificationLog):
        self.settings = settings
        self.whatsapp_client = whatsapp_client
        self.sms_client = sms_client
        self.log = log

    async def notify_signal(self, signal: Signal) -> List[str]:
        """Fan out a signal to every recipient. Returns the list of log row ids."""
        if not self.settings.notifications_enabled:
            logger.debug("Notifications disabled; skipping fan-out")
            return []
        if not self.settings.notification_numbers:
            logger.warning("NOTIFICATION_NUMBERS empty; nobody to notify")
            return []
        if not self._has_sendable_channel():
            logger.warning("No notification channels configured; skipping signal %s", signal.id)
            return []

        body = format_signal_text(signal)
        sms_body = format_signal_sms_text(signal)
        tasks = []
        if self._whatsapp_sendable():
            tasks.extend(
                self._send_whatsapp_one(recipient=r, signal_id=signal.id, body=body, signal=signal)
                for r in self.settings.notification_numbers
            )
        else:
            logger.warning("WhatsApp not allowed/configured; skipping WhatsApp for signal %s", signal.id)

        if self._sms_sendable():
            tasks.extend(
                self._send_sms_one(recipient=r, signal_id=signal.id, body=sms_body)
                for r in self.settings.notification_numbers
            )
        else:
            logger.warning("SMS not allowed/configured; skipping SMS for signal %s", signal.id)

        return await asyncio.gather(*tasks, return_exceptions=False)

    async def send_test(self, recipient: str, body: str) -> List[str]:
        """Send an ad-hoc text message; bypasses signal formatting."""
        tasks = []
        if self._whatsapp_sendable():
            tasks.append(self._send_whatsapp_one(
                recipient=recipient, signal_id=None, body=body, signal=None,
            ))
        if self._sms_sendable():
            tasks.append(self._send_sms_one(
                recipient=recipient, signal_id=None, body=body,
            ))
        if not tasks:
            logger.warning("No notification channels configured; skipping test send to %s", recipient)
            return []
        return await asyncio.gather(*tasks, return_exceptions=False)

    def _whatsapp_sendable(self) -> bool:
        return (
            self.settings.whatsapp_notifications_allowed
            and self.settings.whatsapp_configured
        )

    def _sms_sendable(self) -> bool:
        return (
            self.settings.sms_notifications_allowed
            and self.settings.sms_configured
        )

    def _has_sendable_channel(self) -> bool:
        return self._whatsapp_sendable() or self._sms_sendable()

    async def _send_whatsapp_one(self, *, recipient: str, signal_id: Optional[str],
                                 body: str, signal: Optional[Signal]) -> str:
        use_template = bool(self.settings.whatsapp_template_name) and signal is not None
        message_type = 'template' if use_template else 'text'

        log_id = await self.log.create_pending(
            signal_id=signal_id, recipient=recipient,
            message_type=message_type, body=body, channel='whatsapp',
        )

        try:
            if use_template:
                params = format_signal_template_params(signal)  # type: ignore[arg-type]
                response = await self.whatsapp_client.send_template(
                    to=recipient,
                    template_name=self.settings.whatsapp_template_name,  # type: ignore[arg-type]
                    language_code=self.settings.whatsapp_template_language,
                    body_parameters=params,
                )
            else:
                response = await self.whatsapp_client.send_text(to=recipient, body=body)

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

    async def _send_sms_one(self, *, recipient: str, signal_id: Optional[str],
                            body: str) -> str:
        log_id = await self.log.create_pending(
            signal_id=signal_id, recipient=recipient,
            message_type='text', body=body, channel='sms',
        )

        try:
            await self.sms_client.send_text(to=recipient, text=body)
            await self.log.mark_sent(log_id, wamid=None)
            logger.info(f"SMS sent to {recipient} (log_id={log_id})")
        except SMSError as e:
            error = f"{e} | body={e.response_body}"
            await self.log.mark_failed(log_id, error=error)
            logger.error(f"SMS send failed for {recipient} (log_id={log_id}): {error}")
        except Exception as e:
            await self.log.mark_failed(log_id, error=str(e))
            logger.exception(f"Unexpected SMS notification error for {recipient} (log_id={log_id})")

        return log_id

    @staticmethod
    def _extract_wamid(response: dict) -> Optional[str]:
        messages = response.get('messages') if isinstance(response, dict) else None
        if isinstance(messages, list) and messages:
            return messages[0].get('id')
        return None
