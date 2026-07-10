from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram_metatrader.config import load_config
from telegram_metatrader.jsonl import RuntimeLogs
from telegram_metatrader.models import ExecutionPlan, ExecutionResult
from telegram_metatrader.service import CopierService


def env() -> dict[str, str]:
    return {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "hash",
        "CHAT_ID": "@blacktrade",
        "SYMBOL_MAP_JSON": '{"GOLD":"XAUUSD"}',
    }


class FakeTelegramClient:
    def __init__(self) -> None:
        self.entity = SimpleNamespace(id=555, broadcast=True, title="Blacktrade")
        self.messages = [
            SimpleNamespace(id=10, message="old BUY GOLD", date=datetime(2026, 7, 10, tzinfo=timezone.utc)),
        ]

    async def get_entity(self, chat_id):
        return self.entity

    async def get_messages(self, entity, *, limit, min_id=0, reverse=False):
        rows = [msg for msg in self.messages if msg.id > min_id]
        rows = sorted(rows, key=lambda msg: msg.id, reverse=not reverse)
        return rows[:limit]


class FakeExecutor:
    def __init__(self) -> None:
        self.signals = []

    def execute(self, signal):
        self.signals.append(signal)
        plan = ExecutionPlan(signal=signal, mt5_symbol="XAUUSD", volume=0.01, dry_run=True)
        return ExecutionResult("dry_run", plan, {})


async def test_service_primes_cursor_and_executes_only_new_messages(tmp_path):
    config = load_config(tmp_path, env())
    client = FakeTelegramClient()
    executor = FakeExecutor()
    logs = RuntimeLogs(config.logs_dir)
    service = CopierService(config, client, executor, logs, __import__("logging").getLogger("test"))

    await service.start()
    assert service.cursor is not None
    assert service.cursor.last_seen_message_id == 10

    client.messages.append(
        SimpleNamespace(
            id=11,
            message="BUY GOLD SL 2310 TP1 2330 TP2 2340",
            date=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
    )
    await service.tick()
    await service.tick()

    assert len(executor.signals) == 1
    assert executor.signals[0].message_id == 11
    assert service.counters.signals_detected_total == 1
    assert service.cursor.last_seen_message_id == 11

