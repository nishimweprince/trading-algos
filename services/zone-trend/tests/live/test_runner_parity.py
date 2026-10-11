"""The live hourly path makes the same signals as the backtest replay.

A runner in shadow mode is warmed on the first part of a synthetic series and
then stepped hour by hour through the rest, with bars delivered the way
``fetch_closed`` delivers them. Every signal it records must be one the
backtest's ``replay`` produces at the same hour, and vice versa.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fake_exchange import Reply
from test_lookahead import CFG, frames, synthetic

from zone_trend.config import Timeframes
from zone_trend.core.replay import bars_from_frame, replay
from zone_trend.core.symbol import SymbolCore
from zone_trend.live.runner import Runner
from zone_trend.live.settings import LiveSettings
from zone_trend.live.venues import PaperVenue
from zone_trend.persistence.repo import Repo

H = 3_600_000


class PublicFake:
    """Public endpoints the shadow runner calls; minute data is empty (no fills)."""

    def __init__(self):
        self.mark: dict[str, float] = {}

    async def klines(self, *a, **k):
        return Reply("ok", [])

    async def mark_klines(self, *a, **k):
        return Reply("ok", [])

    async def funding_history(self, *a, **k):
        return Reply("ok", [])

    async def premium_index(self, symbol):
        return Reply("ok", {"markPrice": str(self.mark.get(symbol, 0))})


async def _noop_alert(*a):
    return None


@pytest.mark.parametrize("seed", [7, 8])
async def test_shadow_runner_signals_match_backtest_replay(tmp_path, seed):
    h1, h4, d1 = frames(synthetic(24 * 400, seed))
    cut = int(h1["close_time"].iloc[24 * 250])
    sym = "AAAUSDT"

    expected = {
        s.id
        for _, step in replay(sym, h1, h4, d1, CFG)
        if step.bar.close_time > cut
        for s in step.result.signals
    }

    public = PublicFake()
    settings = LiveSettings(_env_file=None, zt_symbols_csv=sym)
    repo = Repo(f"sqlite:///{tmp_path / 'zt.db'}")
    runner = Runner(
        CFG,
        settings,
        public,
        PaperVenue(public, 500.0, 0.0005, 5),
        repo,
        _noop_alert,
        [sym],
        clock_ms=lambda: 0,
    )

    def upto(df):
        return df[df["close_time"] <= cut]

    core = SymbolCore(sym, CFG)
    for _ in replay(sym, upto(h1), upto(h4), upto(d1), CFG, core=core):
        pass
    runner.cores[sym] = core

    h4_at = {b.close_time: b for b in bars_from_frame(h4)}
    d1_at = {b.close_time: b for b in bars_from_frame(d1)}
    for bar in bars_from_frame(h1[h1["close_time"] > cut]):
        public.mark[sym] = bar.close
        await runner.step(
            bar.close_time, {sym: (bar, h4_at.get(bar.close_time), d1_at.get(bar.close_time))}
        )

    recorded = pd.DataFrame(repo.signals_between(cut + 1, 1 << 62))
    live = set(recorded.loc[recorded["stage"] != "core", "id"]) if len(recorded) else set()
    assert live == expected
    assert expected, "the fixture produced no signals after the cut; pick another seed"


async def test_runner_refuses_sub_hourly_entry(tmp_path):
    cfg = CFG.model_copy(
        update={"timeframes": Timeframes(zones=["4h", "1h"], trend="1h", entry="15m")}
    )
    sym = "AAAUSDT"
    public = PublicFake()
    settings = LiveSettings(_env_file=None, zt_symbols_csv=sym)
    repo = Repo(f"sqlite:///{tmp_path / 'zt.db'}")
    with pytest.raises(SystemExit, match="hourly bars"):
        Runner(
            cfg,
            settings,
            public,
            PaperVenue(public, 500.0, 0.0005, 5),
            repo,
            _noop_alert,
            [sym],
            clock_ms=lambda: 0,
        )
