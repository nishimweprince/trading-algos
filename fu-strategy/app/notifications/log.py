"""Append-only JSONL notification log.

Each lifecycle transition (pending → sent → delivered/read, or pending → failed)
is appended as one JSON object per line. Queries scan the file, group by `id`,
and return the latest snapshot.

Writes are kept off the event loop via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class NotificationRecord:
    id: str
    signal_id: Optional[str]
    recipient: str
    message_type: str
    body: str
    status: str
    wamid: Optional[str]
    error: Optional[str]
    created_at: str
    sent_at: Optional[str]
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.utcnow().isoformat()


class NotificationLog:
    """JSONL-backed notification log."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        directory = os.path.dirname(self.log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Touch the file so subsequent reads always succeed.
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'a', encoding='utf-8'):
                pass
        logger.info(f"Notification log initialized at {self.log_path}")

    async def _append(self, record: NotificationRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False)

        def _write() -> None:
            with open(self.log_path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')

        async with self._lock:
            await asyncio.to_thread(_write)

    async def _read_all(self) -> List[NotificationRecord]:
        def _read() -> List[NotificationRecord]:
            records: List[NotificationRecord] = []
            if not os.path.exists(self.log_path):
                return records
            with open(self.log_path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(NotificationRecord(**json.loads(line)))
                    except Exception as e:
                        logger.warning(f"Skipping malformed line in {self.log_path}: {e}")
            return records

        return await asyncio.to_thread(_read)

    @staticmethod
    def _latest_by_id(records: List[NotificationRecord]) -> List[NotificationRecord]:
        latest: Dict[str, NotificationRecord] = {}
        for r in records:
            latest[r.id] = r  # later writes win
        return list(latest.values())

    async def create_pending(self, *, signal_id: Optional[str], recipient: str,
                             message_type: str, body: str) -> str:
        now = _now()
        record = NotificationRecord(
            id=str(uuid.uuid4()),
            signal_id=signal_id,
            recipient=recipient,
            message_type=message_type,
            body=body,
            status='pending',
            wamid=None,
            error=None,
            created_at=now,
            sent_at=None,
            updated_at=now,
        )
        await self._append(record)
        return record.id

    async def mark_sent(self, log_id: str, wamid: Optional[str]) -> None:
        existing = await self.get(log_id)
        if existing is None:
            logger.warning(f"mark_sent: no prior record for id={log_id}")
            return
        now = _now()
        existing.status = 'sent'
        existing.wamid = wamid
        existing.sent_at = now
        existing.updated_at = now
        await self._append(existing)

    async def mark_failed(self, log_id: str, error: str) -> None:
        existing = await self.get(log_id)
        if existing is None:
            logger.warning(f"mark_failed: no prior record for id={log_id}")
            return
        existing.status = 'failed'
        existing.error = error
        existing.updated_at = _now()
        await self._append(existing)

    async def update_status_by_wamid(self, wamid: str, status: str,
                                     error: Optional[str] = None) -> int:
        """Update status from a webhook event. Returns rows updated (0 or 1)."""
        records = self._latest_by_id(await self._read_all())
        match = next((r for r in records if r.wamid == wamid), None)
        if match is None:
            return 0
        match.status = status
        if error:
            match.error = error
        match.updated_at = _now()
        await self._append(match)
        return 1

    async def list_recent(self, *, limit: int = 100, signal_id: Optional[str] = None,
                          recipient: Optional[str] = None,
                          status: Optional[str] = None) -> List[NotificationRecord]:
        records = self._latest_by_id(await self._read_all())
        if signal_id is not None:
            records = [r for r in records if r.signal_id == signal_id]
        if recipient is not None:
            records = [r for r in records if r.recipient == recipient]
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    async def get(self, log_id: str) -> Optional[NotificationRecord]:
        records = self._latest_by_id(await self._read_all())
        for r in records:
            if r.id == log_id:
                return r
        return None
