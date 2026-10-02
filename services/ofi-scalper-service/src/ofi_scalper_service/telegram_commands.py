"""Inbound Telegram commands on the scalper's own bot: ``/kill`` and ``/status``.

This long-polls ``getUpdates`` on ``OFI_TELEGRAM_BOT_TOKEN``, which must be a
bot of its own: two pollers on one token take turns receiving updates, so
sharing notification-service's bot would lose ``/kill`` half the time.

Only messages from ``OFI_TELEGRAM_ADMIN_USER_IDS`` are acted on. Everything
else, including any text after the command, is data: logged (without its
content) and never interpreted. Acknowledging a halt is deliberately not a
Telegram command; it needs the authenticated HTTP endpoint.

Updates queued while the process was down are dropped on start, so a stale
``/kill`` cannot fire after a restart someone already dealt with.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr
from ta_core.logging_config import log_event

__all__ = ["COMMANDS", "TelegramCommands", "parse_command"]

COMMANDS = frozenset({"kill", "status"})
POLL_SECONDS = 25

Handler = Callable[[str, int], Awaitable[str]]


def parse_command(text: str) -> str | None:
    """``/kill``, ``/kill@my_bot reason`` -> ``kill``; anything else -> None."""
    if not text.startswith("/"):
        return None
    word = text[1:].split(maxsplit=1)[0] if len(text) > 1 else ""
    name = word.split("@", 1)[0].lower()
    return name if name in COMMANDS else None


class TelegramCommands:
    def __init__(
        self,
        token: SecretStr,
        admins: frozenset[int],
        handler: Handler,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = "https://api.telegram.org",
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._admins = admins
        self._handler = handler
        self._http = http or httpx.AsyncClient(timeout=POLL_SECONDS + 10)
        self._owns_http = http is None
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._offset: int | None = None
        self.handled = 0
        self.ignored = 0
        self.errors = 0

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token.get_secret_value()}/{method}"

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            response = await self._http.post(self._url(method), json=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The type only: the URL carries the bot token.
            raise ConnectionError(f"telegram {method}: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise ConnectionError(f"telegram {method}: {body.get('description', 'not ok')}")
        return body["result"]

    async def drop_pending(self) -> None:
        updates = await self._call("getUpdates", offset=-1, timeout=0)
        self._offset = updates[-1]["update_id"] + 1 if updates else None

    async def poll_once(self) -> None:
        params: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        for update in await self._call("getUpdates", **params):
            self._offset = update["update_id"] + 1
            await self._dispatch(update)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        command = parse_command(str(message.get("text") or ""))
        if command is None or user_id not in self._admins:
            self.ignored += 1
            log_event(
                "telegram_command_ignored",
                level=logging.WARNING if command else logging.INFO,
                user_id=user_id,
                recognised=command is not None,
            )
            return
        self.handled += 1
        log_event("telegram_command", command=command, user_id=user_id)
        reply = await self._handler(command, user_id)
        if chat_id is not None:
            try:
                await self._call("sendMessage", chat_id=chat_id, text=reply[:4000])
            except ConnectionError as exc:
                log_event("telegram_reply_failed", level=logging.WARNING, error=str(exc))

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self._offset is None:
                    await self.drop_pending()
                    self._offset = self._offset if self._offset is not None else 0
                await self.poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling
                self.errors += 1
                log_event("telegram_poll_failed", level=logging.WARNING, error=str(exc)[:200])
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def stats(self) -> dict[str, Any]:
        return {"handled": self.handled, "ignored": self.ignored, "errors": self.errors}
