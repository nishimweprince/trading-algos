from __future__ import annotations

from pathlib import Path

import pytest

from backtesting_service.config import Settings

# The committed, credential-scrubbed copy of the configuration this service's
# research ran under (see the file's header). Tests that pin "the shipped
# configuration" read it, not the developer's gitignored .env, so they mean the
# same thing on every machine and in CI.
SHIPPED_ENV = Path(__file__).resolve().parents[1] / "scripts" / "determinism.env"


@pytest.fixture
def shipped_settings() -> Settings:
    return Settings(_env_file=SHIPPED_ENV, _env_file_encoding="utf-8")
