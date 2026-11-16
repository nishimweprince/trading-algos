"""The live code path (Runner: hub candles -> engine -> orders -> broker sync) reproduces the replay.

Drives the real Runner minute by minute against FakeHub serving the packed history as broker candles, then compares
its trades with replay_symbol over the same window. Needs WF_DATA (parity marker).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import DATA
from fakehub import FakeHub, minutes

from walkforward_patterns import config as cfgmod
from walkforward_patterns.clock import parse, utc_to_research
from walkforward_patterns.journal import Journal
from walkforward_patterns.live import Runner
from walkforward_patterns.replay import DEFAULT_CONFIG, load_npz, replay_symbol
from walkforward_patterns.settings import Settings

pytestmark = [pytest.mark.parity, pytest.mark.skipif(not DATA, reason="WF_DATA unset")]

SYM = "USDJPY"
START = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)
END = datetime(2024, 4, 1, 0, 0, tzinfo=UTC)


def test_live_runner_matches_replay(tmp_path: Path):
    full = cfgmod.load(DEFAULT_CONFIG)
    cfg = replace(full, strategies=tuple(s for s in full.strategies if s.symbol == SYM))
    t, o, h, l, c = load_npz(Path(DATA) / f"{SYM}_m1.npz")
    lo = parse("2023-08-01 00:00")
    hi = utc_to_research(END + timedelta(days=3))
    m = (t >= lo) & (t < hi)
    hub = FakeHub.from_research({SYM: (t[m], o[m], h[m], l[m], c[m])})
    hub.advance(START)
    settings = Settings(WFP_DB=str(tmp_path / "j.sqlite3"), WFP_BAR_WAIT_SECONDS=0, WFP_WARMUP_DAYS=175,
                        WFP_HUB_API_KEY="x")
    j = Journal(settings.db_path)
    runner = Runner(settings, cfg, hub, j)
    runner.warmup(START)
    for now in minutes(START, END):
        now = now + timedelta(seconds=2)
        hub.advance(now)
        runner.tick(now)
    live = {}
    for r in j.closed_trades():
        live[(r["strategy"], int(r["entry_minute"]))] = float(r["r_price"])

    # replay with the same history start as the live warm-up
    warm_start = utc_to_research(START - timedelta(days=175))
    trades, _ = replay_symbol(SYM, Path(DATA), cfg, start=warm_start, end=utc_to_research(END), progress=False)
    # the live run starts flat; positions the replay carried into the window last up to 48h, so skip 3 days
    t0, t1 = utc_to_research(START + timedelta(days=3)), utc_to_research(END - timedelta(days=3))
    ref = {(sid, p.t_entry): p.R + p.cost / p.risk  # price R (the fake hub charges no research cost)
           for sid, rows in trades.items() for p in rows if t0 <= p.t_entry < t1}
    live_in = {k: v for k, v in live.items() if t0 <= k[1] < t1}
    assert len(ref) > 20
    common = ref.keys() & live_in.keys()
    recall = len(common) / len(ref)
    precision = len(common) / len(live_in)
    diffs = np.array([abs(ref[k] - live_in[k]) for k in common])
    print(f"ref {len(ref)} live {len(live_in)} common {len(common)} max dR {diffs.max():.4f}")
    assert recall >= 0.97 and precision >= 0.97, (sorted(ref.keys() - live_in.keys())[:5],
                                                  sorted(live_in.keys() - ref.keys())[:5])
    assert (diffs <= 0.01).mean() >= 0.97
