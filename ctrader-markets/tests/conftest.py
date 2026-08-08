from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings

ENV_TEMPLATE = """\
CTRADER_CLIENT_ID=test-client-id
CTRADER_CLIENT_SECRET=test-client-secret
CTRADER_ACCESS_TOKEN=test-access-token
CTRADER_REFRESH_TOKEN=test-refresh-token
CTRADER_ACCOUNT_ID=12345678
CTRADER_ENVIRONMENT=demo
API_KEY=test-api-key-at-least-16
HOST=127.0.0.1
PORT=8010
SYMBOLS=EURUSD,XAUUSD
LIVE_TRENDBAR_PERIODS=M1
LOG_LEVEL=INFO
"""


def build_settings(tmp_path: Path, **overrides: object) -> Settings:
    """A valid Settings without touching the filesystem-resolved .env lookup."""
    values: dict[str, object] = {
        "CTRADER_CLIENT_ID": "test-client-id",
        "CTRADER_CLIENT_SECRET": "test-client-secret",
        "CTRADER_ACCESS_TOKEN": "test-access-token",
        "CTRADER_REFRESH_TOKEN": "test-refresh-token",
        "CTRADER_ACCOUNT_ID": 12345678,
        "CTRADER_ENVIRONMENT": "demo",
        "API_KEY": "test-api-key-at-least-16",
        "SYMBOLS": "EURUSD,XAUUSD",
        "LIVE_TRENDBAR_PERIODS": "M1",
        "TOKEN_CACHE_PATH": tmp_path / "token-cache.json",
        "EVENTS_LOG_PATH": tmp_path / "events.jsonl",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def api_key() -> str:
    return "test-api-key-at-least-16"
