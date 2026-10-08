from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from ta_plugin_binance_futures.account import AccountReader
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.streams import FuturesStreams
from ta_plugin_binance_futures.testing import FakeBinanceFutures, FakeFuturesStream

from ofi_scalper_service.alerts import Alerts
from ofi_scalper_service.config import Settings
from ofi_scalper_service.recorder import Recorder
from ofi_scalper_service.regime_gate import RegimeGate
from ofi_scalper_service.risk import RiskLimits, RiskState
from ofi_scalper_service.runtime import ScalperRuntime

API_KEY = "test-api-key-at-least-16"
AUTH = {"X-API-Key": API_KEY}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "API_KEY": API_KEY,
        "BINANCE_FUTURES_SYMBOLS": "BTCUSDT,ETHUSDT",
        "BINANCE_FUTURES_REST_URL": "https://fapi.binance.test",
        "BINANCE_FUTURES_WS_URL": "wss://fstream.binance.test",
        "BINANCE_FUTURES_RECONNECT_MAX_BACKOFF_SECONDS": 0.01,
        "OFI_MAX_POSITION_NOTIONAL_USD": 500,
        "OFI_MAX_TOTAL_NOTIONAL_USD": 800,
        "OFI_DAILY_LOSS_LIMIT_USD": 50,
        "OFI_MANUAL_APPROVAL_NOTIONAL_USD": 400,
        "OFI_RECORD_DIR": str(tmp_path / "raw"),
        "OFI_KILL_FILE_PATH": str(tmp_path / "KILL"),
        "EVENTS_LOG_PATH": str(tmp_path / "events.jsonl"),
        "OFI_HOST_TAG": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    async def send(self, subject: str, lines: list[str], **_: Any) -> None:
        self.sent.append((subject, lines))


class Clock:
    def __init__(self, start: int = 1_790_000_000_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        self.now += 1_000  # 1 µs per read keeps stamps strictly increasing
        return self.now


@pytest.fixture
def fake() -> FakeBinanceFutures:
    return FakeBinanceFutures()


@pytest.fixture
async def build(
    tmp_path: Path, fake: FakeBinanceFutures
) -> AsyncIterator[Callable[..., ScalperRuntime]]:
    created: list[tuple[ScalperRuntime, httpx.AsyncClient, FakeFuturesStream]] = []

    def factory(
        stream: FakeFuturesStream, *, record: bool = False, **overrides: Any
    ) -> ScalperRuntime:
        settings = make_settings(tmp_path, OFI_RECORD_ENABLED=record, **overrides)
        http = httpx.AsyncClient(
            base_url=settings.binance_futures_rest_url, transport=httpx.MockTransport(fake.handler)
        )
        rest = FapiRest(settings, http=http)
        clock = Clock()
        recorder = Recorder(settings.record_dir, host_tag="test") if record else None
        streams = FuturesStreams(
            settings,
            rest=rest,
            ws_connect=stream,
            clock_ns=clock,
            on_raw=recorder.write if recorder else None,
        )
        runtime = ScalperRuntime(
            settings,
            streams=streams,
            account=AccountReader(rest),
            risk=RiskState(RiskLimits.from_settings(settings)),
            gate=RegimeGate.from_settings(settings),
            alerts=Alerts(RecordingNotifier()),
            recorder=recorder,
            clock_ns=clock,
        )
        created.append((runtime, http, stream))
        return runtime

    yield factory
    for runtime, http, stream in created:
        stream.release.set()
        await runtime.close()
        await http.aclose()


async def until(predicate: Callable[[], bool], seconds: float = 2.0) -> None:
    """Poll a condition the code under test exposes no event for."""
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - polling test state is the point
            await asyncio.sleep(0.005)
