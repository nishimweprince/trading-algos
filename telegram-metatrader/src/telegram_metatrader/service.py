from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .jsonl import RuntimeLogs
from .models import ParseFailure, Signal
from .mt5_executor import Mt5Executor
from .parser import parse_signal_text
from .state import CursorState, Deduper, load_cursor, save_cursor
from .telegram_client import ResolvedChat, latest_message_id, messages_since, resolve_chat


@dataclass
class Counters:
    messages_polled_total: int = 0
    signals_detected_total: int = 0
    execution_attempts_total: int = 0
    execution_failures_total: int = 0
    parser_rejections_total: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "messages_polled_total": self.messages_polled_total,
            "signals_detected_total": self.signals_detected_total,
            "execution_attempts_total": self.execution_attempts_total,
            "execution_failures_total": self.execution_failures_total,
            "parser_rejections_total": self.parser_rejections_total,
        }


class CopierService:
    def __init__(
        self,
        config: AppConfig,
        client: Any,
        executor: Mt5Executor,
        logs: RuntimeLogs,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.client = client
        self.executor = executor
        self.logs = logs
        self.logger = logger
        self.counters = Counters()
        self.chat: ResolvedChat | None = None
        self.cursor: CursorState | None = None
        self.deduper = Deduper(config.handled_path)

    async def start(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self.chat = await resolve_chat(self.client, self.config.chat_id)
        self.cursor = load_cursor(self.config.cursor_path, self.chat.stable_id)
        if self.cursor.last_seen_message_id == 0:
            latest = await latest_message_id(self.client, self.chat.entity)
            self.cursor.last_seen_message_id = latest
            save_cursor(self.config.cursor_path, self.cursor)
            self.logger.info("Primed cursor for %s at message %s", self.chat.title, latest)

    async def run_forever(self) -> None:
        if self.chat is None or self.cursor is None:
            await self.start()
        while True:
            await self.tick()
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def tick(self) -> None:
        if self.chat is None or self.cursor is None:
            await self.start()
        assert self.chat is not None
        assert self.cursor is not None

        try:
            messages = await messages_since(
                self.client,
                self.chat.entity,
                self.cursor.last_seen_message_id,
                self.config.poll_max_messages,
            )
        except Exception as exc:
            self.logs.errors.append({"kind": "telegram_poll_failed", "error": str(exc)})
            self.logger.exception("Telegram poll failed")
            return

        self.counters.messages_polled_total += len(messages)
        for msg in messages:
            if self.deduper.seen(self.chat.stable_id, msg.id):
                self.cursor.last_seen_message_id = max(self.cursor.last_seen_message_id, msg.id)
                continue
            if self.config.log_raw_messages:
                self.logs.raw.append(
                    {
                        "chat": self.chat.stable_id,
                        "message_id": msg.id,
                        "message_date": msg.date,
                        "text": msg.text,
                    }
                )

            parsed = parse_signal_text(
                msg.text,
                source_chat=self.chat.stable_id,
                message_id=msg.id,
                message_date=msg.date,
            )
            if isinstance(parsed, ParseFailure):
                self.counters.parser_rejections_total += 1
                if parsed.reason in {"ambiguous_direction", "unknown_symbol"}:
                    self.logs.errors.append(
                        {
                            "kind": "parser_rejected",
                            "reason": parsed.reason,
                            "message_id": msg.id,
                            "text": msg.text,
                        }
                    )
                self._advance(msg.id)
                continue

            await self._handle_signal(parsed)
            self._advance(msg.id)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "live" if self.config.live_trading_enabled else "dry_run",
            "chat": None
            if self.chat is None
            else {
                "input": self.chat.input_value,
                "stable_id": self.chat.stable_id,
                "title": self.chat.title,
            },
            "cursor": None if self.cursor is None else self.cursor.to_json(),
            "counters": self.counters.to_json(),
        }

    async def _handle_signal(self, signal: Signal) -> None:
        self.counters.signals_detected_total += 1
        self.logs.signals.append(signal.to_json())
        self.counters.execution_attempts_total += 1
        try:
            result = self.executor.execute(signal)
            self.logs.executions.append(result.to_json())
            if result.status in {"rejected", "failed"}:
                self.counters.execution_failures_total += 1
        except Exception as exc:
            self.counters.execution_failures_total += 1
            self.logs.errors.append(
                {
                    "kind": "execution_failed",
                    "error": str(exc),
                    "signal": signal.to_json(),
                }
            )
            self.logger.exception("Execution failed")

    def _advance(self, message_id: int) -> None:
        assert self.chat is not None
        assert self.cursor is not None
        self.deduper.mark(self.chat.stable_id, message_id)
        self.cursor.last_seen_message_id = max(self.cursor.last_seen_message_id, message_id)
        save_cursor(self.config.cursor_path, self.cursor)

