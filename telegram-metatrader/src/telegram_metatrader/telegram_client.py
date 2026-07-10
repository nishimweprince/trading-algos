from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig


@dataclass(frozen=True)
class TelegramMessage:
    id: int
    text: str
    date: str


@dataclass(frozen=True)
class ResolvedChat:
    input_value: str
    stable_id: str
    title: str
    entity: Any


def normalize_chat_id(chat_id: str) -> str | int:
    value = chat_id.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    if value.startswith("@"):
        return value
    return f"@{value}"


def session_name(config: AppConfig) -> str:
    if config.telegram_session_string:
        return config.telegram_session_string
    return str(config.telegram_session_path)


async def create_client(config: AppConfig) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session: str | StringSession
    if config.telegram_session_string:
        session = StringSession(config.telegram_session_string)
    else:
        config.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
        session = str(config.telegram_session_path)
    client = TelegramClient(session, config.telegram_api_id, config.telegram_api_hash)
    await client.start(phone=config.telegram_phone_number)
    return client


async def resolve_chat(client: Any, chat_id: str) -> ResolvedChat:
    normalized = normalize_chat_id(chat_id)
    entity = await client.get_entity(normalized)
    stable = str(getattr(entity, "id", normalized))
    if stable and not stable.startswith("-") and getattr(entity, "broadcast", False):
        stable = f"-100{stable}"
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(normalized)
    return ResolvedChat(input_value=chat_id, stable_id=stable, title=title, entity=entity)


async def latest_message_id(client: Any, entity: Any) -> int:
    messages = await client.get_messages(entity, limit=1)
    if not messages:
        return 0
    return int(messages[0].id)


async def messages_since(client: Any, entity: Any, min_id: int, limit: int) -> list[TelegramMessage]:
    messages = await client.get_messages(entity, min_id=min_id, limit=limit, reverse=True)
    out: list[TelegramMessage] = []
    for msg in messages:
        text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
        out.append(
            TelegramMessage(
                id=int(msg.id),
                text=text,
                date=_date_to_iso(getattr(msg, "date", None)),
            )
        )
    return out


def session_file_exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _date_to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return datetime.now(timezone.utc).isoformat()

