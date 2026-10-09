"""The runner heartbeat is written with the real weight limiter attached.

Regression: ``_heartbeat_loop`` called ``limiter.used()``, but
``WeightLimiter.used`` is a property. Every heartbeat then raised TypeError
inside the loop's blanket except, so no heartbeat was ever written and
``/health`` never went healthy.
"""

from __future__ import annotations

import asyncio
import contextlib

from ta_plugin_binance_futures.limiter import WeightLimiter
from test_lookahead import CFG

from zone_trend.live.runner import Runner
from zone_trend.live.settings import LiveSettings
from zone_trend.live.venues import PaperVenue
from zone_trend.persistence.repo import Repo


class _Trading:
    def __init__(self):
        from types import SimpleNamespace

        self.rest = SimpleNamespace(limiter=WeightLimiter(1200))


async def _noop_alert(*a):
    return None


async def test_heartbeat_loop_writes_with_real_limiter(tmp_path):
    ex = _Trading()
    repo = Repo(f"sqlite:///{tmp_path / 'zt.db'}")
    settings = LiveSettings(_env_file=None, zt_symbols_csv="BTCUSDT")
    runner = Runner(
        CFG,
        settings,
        ex,
        PaperVenue(ex, 500.0, 0.0005, 5),
        repo,
        _noop_alert,
        ["BTCUSDT"],
        clock_ms=lambda: 1_700_000_000_000,
    )
    task = asyncio.create_task(runner._heartbeat_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert repo.get_state("heartbeat") == 1_700_000_000_000
    assert repo.get_state("metrics")["api_weight_used"] == 0
