from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        enriched = {"ts": utc_now(), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(enriched, default=str, ensure_ascii=True))
            handle.write("\n")


class RuntimeLogs:
    def __init__(self, logs_dir: Path) -> None:
        self.raw = JsonlLogger(logs_dir / "raw.jsonl")
        self.signals = JsonlLogger(logs_dir / "signals.jsonl")
        self.executions = JsonlLogger(logs_dir / "executions.jsonl")
        self.errors = JsonlLogger(logs_dir / "errors.jsonl")

