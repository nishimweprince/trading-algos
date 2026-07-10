from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonl import utc_now


@dataclass
class CursorState:
    chat_id: str
    last_seen_message_id: int = 0
    updated_at: str = field(default_factory=utc_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "last_seen_message_id": self.last_seen_message_id,
            "updated_at": self.updated_at,
        }


def load_cursor(path: Path, chat_id: str) -> CursorState:
    data = _read_json(path, default=None)
    if not isinstance(data, dict) or data.get("chat_id") != chat_id:
        return CursorState(chat_id=chat_id)
    return CursorState(
        chat_id=chat_id,
        last_seen_message_id=int(data.get("last_seen_message_id", 0)),
        updated_at=str(data.get("updated_at", utc_now())),
    )


def save_cursor(path: Path, cursor: CursorState) -> None:
    cursor.updated_at = utc_now()
    _write_json(path, cursor.to_json())


class Deduper:
    def __init__(self, path: Path, max_items: int = 2000) -> None:
        self.path = path
        self.max_items = max_items
        data = _read_json(path, default=[])
        self._keys: list[str] = [str(x) for x in data] if isinstance(data, list) else []
        self._set = set(self._keys)

    def seen(self, chat_key: str, message_id: int) -> bool:
        return self._key(chat_key, message_id) in self._set

    def mark(self, chat_key: str, message_id: int) -> None:
        key = self._key(chat_key, message_id)
        if key in self._set:
            return
        self._keys.append(key)
        self._set.add(key)
        if len(self._keys) > self.max_items:
            removed = self._keys[: len(self._keys) - self.max_items]
            self._keys = self._keys[-self.max_items :]
            for item in removed:
                self._set.discard(item)
        _write_json(self.path, self._keys)

    @staticmethod
    def _key(chat_key: str, message_id: int) -> str:
        return f"{chat_key}:{message_id}"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    tmp.replace(path)

